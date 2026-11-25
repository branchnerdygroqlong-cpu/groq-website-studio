import json
import os
import time

from groq import Groq

from .config import MODEL_LIMITS, DEFAULT_LIMITS
from .storage import Store


def _env_int(name, default):
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


# All tunable centrally; override with environment variables.
RETRY_WAIT_SECONDS = _env_int("STUDIO_RETRY_WAIT", 60)          # wait before retrying
MAX_RATE_RETRIES = _env_int("STUDIO_MAX_RATE_RETRIES", 5)       # per API request
MAX_CONTINUATIONS = _env_int("STUDIO_MAX_CONTINUATIONS", 2)     # per truncated output
CONTINUATION_WAIT_SECONDS = _env_int("STUDIO_CONTINUATION_WAIT", 0)   # quota-aware waiting already pauses when needed
TASK_TOKEN_BUDGET = _env_int("STUDIO_TASK_TOKEN_BUDGET", 7000)      # per model, per section/CSS part
BUILD_TOKEN_BUDGET = _env_int("STUDIO_BUILD_TOKEN_BUDGET", 80000)     # hard stop for one build
MIN_USEFUL_FRACTION = 0.7   # never run a call with far less output room than requested

# Reasoning models spend max_tokens on hidden "thinking"; keep that small.
MODEL_EXTRA = {
    "openai/gpt-oss-20b": {"reasoning_effort": "low"},
    "openai/gpt-oss-120b": {"reasoning_effort": "low"},
    "qwen/qwen3.8-27b": {"reasoning_effort": "none"},
}
MAX_REPAIRS = _env_int("STUDIO_MAX_REPAIRS", 2)                 # per generated artifact


class QuotaWindowError(RuntimeError):
    """Local per-minute quota is exhausted; waiting will help."""


class DailyQuotaError(RuntimeError):
    """Daily quota is exhausted; waiting a minute will NOT help."""


class BudgetExceededError(RuntimeError):
    """The per-build token budget was used up; stop instead of burning quota."""


class TaskBudgetError(RuntimeError):
    """One model used its share of tokens on one task; try the next model."""


class GenerationError(RuntimeError):
    """A validated generation failed on every model after retries/repairs."""


def estimate_tokens(messages, tools=None):
    payload = json.dumps(messages, default=str)
    if tools:
        payload += json.dumps(tools, default=str)
    # Conservative approximation; API usage returned after the call is authoritative.
    return max(1, len(payload) // 4)


def _finish_reason(response):
    try:
        return response.choices[0].finish_reason
    except (AttributeError, IndexError, TypeError):
        return None


def _is_retryable_api_error(exc):
    """429s, 5xx, timeouts and connection errors. Daily-limit 429s are not retryable."""
    text = str(exc).lower()
    if "per day" in text or "tokens per day" in text or "(tpd)" in text or "requests per day" in text:
        return False
    status = getattr(exc, "status_code", None)
    if status == 429 or (isinstance(status, int) and status >= 500):
        return True
    return type(exc).__name__ in {"APIConnectionError", "APITimeoutError", "RateLimitError"}


def _retry_after(exc):
    try:
        value = exc.response.headers.get("retry-after")
        return float(value) if value else 0.0
    except Exception:
        return 0.0


def _sleep(seconds, reason):
    seconds = int(max(1, seconds))
    print(f"[router] {reason}; waiting {seconds}s ...", flush=True)
    time.sleep(seconds)


class GroqRouter:
    """Central Groq client: fallback, local quota accounting, retry, continuation, repair."""

    def __init__(self, store=None, client=None, quota_safety_tokens=100):
        key = os.getenv("GROQ_API_KEY")
        if client is None and not key:
            raise RuntimeError(
                "GROQ_API_KEY is not set. Export it before making Groq calls."
            )
        self.client = client or Groq(api_key=key)
        self.store = store or Store()
        self.quota_safety_tokens = max(0, int(quota_safety_tokens))
        self.build_budget = BUILD_TOKEN_BUDGET
        self._spent = 0
        self._task_cap = None
        self._extras_disabled = set()

    def reset_budget(self):
        self._spent = 0

    def limits(self, model):
        return MODEL_LIMITS.get(model, DEFAULT_LIMITS)

    # ------------------------------------------------------------------ #
    # Single request, no retry
    # ------------------------------------------------------------------ #
    def _chat_once(self, model, messages, tools=None, estimated_out_tokens=1000,
                   fallback_models=None):
        if self._spent >= self.build_budget:
            raise BudgetExceededError(
                f"Build token budget ({self.build_budget}) reached; stopping. Raise "
                "STUDIO_BUILD_TOKEN_BUDGET if this is intended.")
        if self._task_cap is not None and self._spent >= self._task_cap:
            raise TaskBudgetError("This model used its token allowance for this task.")
        input_tokens = estimate_tokens(messages, tools)
        candidates = [model] + (fallback_models or [])
        selected = None
        selected_max_tokens = 0
        window_blocked = False  # at least one candidate is blocked only by a minute window

        for candidate in candidates:
            lim = self.limits(candidate)
            if tools and not lim.get("supports_tools", False):
                continue

            usage = self.store.usage(candidate)
            if (usage["daily_requests"] >= lim["rpd"]
                    or usage["daily_tokens"] + input_tokens >= lim["tpd"]):
                continue
            if usage["minute_requests"] >= lim["rpm"]:
                window_blocked = True
                continue

            minute_output_budget = (
                lim["tpm"] - usage["minute_tokens"] - input_tokens
                - self.quota_safety_tokens
            )
            if lim.get("otpm"):
                minute_output_budget = min(
                    minute_output_budget, lim["otpm"] - usage["minute_completion_tokens"])
            daily_output_budget = lim["tpd"] - usage["daily_tokens"] - input_tokens
            wanted = min(max(0, int(estimated_out_tokens)), max(0, int(lim["max_out"])))
            max_tokens = min(
                wanted,
                max(0, int(minute_output_budget)),
                max(0, int(daily_output_budget)),
            )
            floor = max(64, int(wanted * MIN_USEFUL_FRACTION))
            if max_tokens < floor:
                # Do not run with a tiny output allowance: it only causes truncation.
                if daily_output_budget >= floor and minute_output_budget < floor:
                    if input_tokens + self.quota_safety_tokens + floor <= lim["tpm"]:
                        window_blocked = True
                continue

            selected = candidate
            selected_max_tokens = max_tokens
            break

        if selected is None:
            if window_blocked:
                raise QuotaWindowError(
                    "Per-minute quota exhausted for all candidate models."
                )
            raise DailyQuotaError(
                "No candidate model has usable quota (daily limit reached, or the "
                "prompt is larger than the model's per-minute token limit)."
            )

        kwargs = {"model": selected, "messages": messages, "max_tokens": selected_max_tokens}
        if tools:
            kwargs.update(tools=tools, tool_choice="auto")

        extra = MODEL_EXTRA.get(selected)
        if extra and selected not in self._extras_disabled:
            kwargs["extra_body"] = dict(extra)
        try:
            response = self.client.chat.completions.create(**kwargs)
        except Exception as exc:
            text = str(exc).lower()
            if "extra_body" in kwargs and getattr(exc, "status_code", None) == 400 and any(
                    k in text for k in ("reasoning", "unsupported", "unrecognized", "additional properties")):
                print(f"[router] {selected} rejected {extra}; retrying without it", flush=True)
                self._extras_disabled.add(selected)
                kwargs.pop("extra_body")
                response = self.client.chat.completions.create(**kwargs)
            else:
                raise

        api_usage = getattr(response, "usage", None)
        prompt_tokens = getattr(api_usage, "prompt_tokens", None)
        completion_tokens = getattr(api_usage, "completion_tokens", None)
        total_tokens = getattr(api_usage, "total_tokens", None)
        if prompt_tokens is None:
            prompt_tokens = input_tokens
        if completion_tokens is None:
            completion_tokens = selected_max_tokens
        if total_tokens is None:
            total_tokens = prompt_tokens + completion_tokens

        self.store.record_usage(
            selected, time.time(), int(total_tokens),
            prompt_tokens=int(prompt_tokens), completion_tokens=int(completion_tokens),
        )
        self._spent += int(total_tokens)
        print(f"[router] {selected}: {int(prompt_tokens)} in + {int(completion_tokens)} out "
              f"(build total {self._spent}/{self.build_budget})", flush=True)
        return response

    # ------------------------------------------------------------------ #
    # Request with retry (rate limits, transient errors, local minute quota)
    # ------------------------------------------------------------------ #
    def chat(self, model, messages, tools=None, estimated_out_tokens=1000,
             fallback_models=None):
        attempt = 0
        while True:
            try:
                return self._chat_once(model, messages, tools, estimated_out_tokens,
                                       fallback_models)
            except QuotaWindowError as exc:
                reason = "local minute quota exhausted"
                wait = RETRY_WAIT_SECONDS
                err = exc
            except DailyQuotaError:
                raise
            except Exception as exc:
                if not _is_retryable_api_error(exc):
                    raise
                reason = f"{type(exc).__name__} (rate limit / transient error)"
                wait = max(RETRY_WAIT_SECONDS, _retry_after(exc) + 1)
                err = exc
            attempt += 1
            if attempt > MAX_RATE_RETRIES:
                raise RuntimeError(
                    f"Gave up after {MAX_RATE_RETRIES} retries: {err}"
                ) from err
            _sleep(wait, f"{reason}, retry {attempt}/{MAX_RATE_RETRIES}")

    # ------------------------------------------------------------------ #
    # Complete output: continue truncated responses
    # ------------------------------------------------------------------ #
    def _complete_full(self, model, messages, estimated_out_tokens):
        parts, convo = [], list(messages)
        out, bumps, continuations = estimated_out_tokens, 0, 0
        cap = self.limits(model)["max_out"]
        while True:
            response = self.chat(model, convo, estimated_out_tokens=out, fallback_models=[])
            try:
                text = response.choices[0].message.content or ""
            except (AttributeError, IndexError, TypeError) as exc:
                raise RuntimeError("Groq returned an empty or unrecognized completion.") from exc
            if _finish_reason(response) != "length":
                parts.append(text)
                return "".join(parts)
            if not text.strip():
                # Whole allowance went on hidden reasoning: nothing to continue from.
                if bumps >= 2 or out >= cap:
                    raise RuntimeError(
                        "Model used its whole output allowance without visible text "
                        "(reasoning overhead).")
                bumps += 1
                out = min(cap, out * 2)
                print(f"[router] no visible output (reasoning overhead); retrying with "
                      f"{out} output tokens", flush=True)
                continue
            parts.append(text)
            continuations += 1
            if continuations > MAX_CONTINUATIONS:
                raise RuntimeError(
                    f"Output still truncated after {MAX_CONTINUATIONS} continuations.")
            if CONTINUATION_WAIT_SECONDS > 0:
                _sleep(CONTINUATION_WAIT_SECONDS,
                       f"output truncated, continuation {continuations}/{MAX_CONTINUATIONS}")
            else:
                print(f"[router] output truncated, continuation "
                      f"{continuations}/{MAX_CONTINUATIONS}", flush=True)
            convo = list(messages) + [
                {"role": "assistant", "content": "".join(parts)},
                {"role": "user", "content": (
                    "Your previous reply was cut off by the output limit. Continue "
                    "EXACTLY where it stopped. Do not repeat anything already written, "
                    "do not restart, and do not add commentary. Output only the "
                    "remaining text.")},
            ]

    # ------------------------------------------------------------------ #
    # Validated generation: retry + continue + model-driven repair
    # ------------------------------------------------------------------ #
    def generate(self, models, messages, validate, *, estimated_out_tokens=800,
                 label="output", clean=None, task_budget=None):
        """Return (text, model). `validate(text)` must raise ValueError when invalid.

        Per model: retry rate limits, continue truncation, then ask the model to repair
        invalid output up to MAX_REPAIRS times. Moves to the next model only when a model
        cannot produce valid output. Never substitutes canned content; raises
        GenerationError if every model fails.
        """
        clean = clean or (lambda t: (t or "").strip())
        errors = []
        for model in models:
            self._task_cap = self._spent + (task_budget or TASK_TOKEN_BUDGET)
            try:
                text = clean(self._complete_full(model, messages, estimated_out_tokens))
                for repair in range(MAX_REPAIRS + 1):
                    try:
                        validate(text)
                        print(f"[router] {label}: ok ({model}"
                              f"{', repaired x%d' % repair if repair else ''})", flush=True)
                        self._task_cap = None
                        return text, model
                    except ValueError as exc:
                        if repair == MAX_REPAIRS:
                            raise
                        print(f"[router] {label}: invalid ({exc}); asking {model} to repair "
                              f"{repair + 1}/{MAX_REPAIRS}", flush=True)
                        repair_msgs = list(messages) + [
                            {"role": "assistant", "content": text[:7000]},
                            {"role": "user", "content": (
                                f"That output failed validation: {exc}\n"
                                "Return the COMPLETE corrected output, fixing the "
                                "problem. Same format rules as before. Output only "
                                "the corrected code, no commentary.")},
                        ]
                        text = clean(self._complete_full(model, repair_msgs,
                                                         estimated_out_tokens))
            except BudgetExceededError:
                raise
            except Exception as exc:
                errors.append(f"{model}: {exc}")
                print(f"[router] {label}: {model} failed: {exc}", flush=True)
        self._task_cap = None
        raise GenerationError(f"{label} failed on every model: " + " | ".join(errors))
