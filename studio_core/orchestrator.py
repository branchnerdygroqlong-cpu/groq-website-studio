import re
from pathlib import Path
from .config import PROJECTS_DIR
from .storage import Store


def name_from_brief(brief, max_words=6):
    """A readable site name taken from the description, used when no name is typed."""
    text = " ".join(str(brief or "").split())
    match = re.search(
        r"\b(?:home\s?page|web\s?site|site|landing page|web page|page)\s+(?:for|about|of)\s+"
        r"(?:the\s+|a\s+|an\s+)?(.+)", text, re.I)
    if not match:
        match = re.search(r"\b(?:about|on)\s+(.+)", text, re.I)
    candidate = (match.group(1) if match else text)
    candidate = re.split(r"[,:;.\n(]| - | \u2013 | \u2014 ", candidate, maxsplit=1)[0]
    words = re.sub(r"[^\w\s'&-]", "", candidate).split()[:max_words]
    name = " ".join(w[:1].upper() + w[1:] for w in words).strip(" -'&")
    return name or "New Website"


class ProjectOrchestrator:
    """Creates projects and tracks independently executable work items."""
    def __init__(self, store=None):
        self.store = store or Store()

    def create_project(self, name, business_type="", brief=""):
        # The folder is "<name>-<project number>", so two sites with the same name never
        # share a folder (a rebuild of the same project still reuses its own folder).
        slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "new-project"
        project_id = self.store.create_project(name, business_type, brief, "")
        folder = Path(PROJECTS_DIR) / f"{slug}-{project_id}"
        folder.mkdir(parents=True, exist_ok=True)
        with self.store.connect() as db:
            db.execute("UPDATE projects SET root_path=? WHERE id=?", (str(folder), project_id))
        return self.store.get_project(project_id)

    def add_task(self, project_id, title, description="", agent=""):
        if not self.store.get_project(project_id):
            raise ValueError(f"Project {project_id} not found")
        return self.store.add_task(project_id, title, description, agent)

    def project_status(self, project_id):
        project = self.store.get_project(project_id)
        if not project:
            raise ValueError(f"Project {project_id} not found")
        return {"project": project, "tasks": self.store.list_tasks(project_id)}

    def run_task(self, project_id, task_id, worker):
        tasks = self.store.list_tasks(project_id)
        task = next((t for t in tasks if t["id"] == task_id), None)
        if task is None:
            raise ValueError(f"Task {task_id} not found in project {project_id}")
        self.store.update_task(task_id, "running")
        try:
            result = worker(task)
            self.store.update_task(task_id, "completed", str(result))
            return result
        except Exception as exc:
            self.store.update_task(task_id, "failed", str(exc))
            raise
