# Studio update: multi-page fix, contact/social, shopping cart, Stripe, USPS

## Where each file goes

| File | Goes in | New or replaces |
|---|---|---|
| `studio_gui.py` | `~/groq-website-studio/` | replaces yours |
| `check_site.py` | `~/groq-website-studio/` | replaces yours |
| `shop_cart.py` | `~/groq-website-studio/` | new |
| `shop_api.py` | `~/groq-website-studio/` | new |
| `shop_server.py` | `~/groq-website-studio/` | new |
| `plugin_stripe.py` | `~/groq-website-studio/` | new |
| `plugin_usps.py` | `~/groq-website-studio/` | new |
| `secrets_env.py` | `~/groq-website-studio/` | new |
| `page_expander.py` | `~/groq-website-studio/studio_core/` | replaces yours (use this one, not `page_expander_1.py`) |
| `site_extras.py` | `~/groq-website-studio/studio_core/` | new |

## Install

```bash
cd ~/groq-website-studio
source venv/bin/activate
mkdir -p ~/studio-backup && cp studio_gui.py check_site.py studio_core/page_expander.py ~/studio-backup/
unzip -o ~/Downloads/studio-update.zip -d ~/groq-website-studio
python -m py_compile studio_gui.py shop_cart.py shop_api.py shop_server.py plugin_stripe.py plugin_usps.py secrets_env.py studio_core/*.py && echo COMPILES
./start_studio.sh
```

No new packages are needed (Python standard library only). To undo: copy the three backed-up files
back and delete the new files.

## 1. Pages that link to each other

* Navigation, header and footer links are now planned by plain code: **Home goes to the main page,
  every other link gets its own page.** The AI is only used to write each page's text and to plan links
  inside the page body.
* If the AI's body-link plan fails, the navigation pages are still created (before, the whole step stopped).
* Every page now links back: the logo goes to `index.html`, each page opens with "Back to the main page",
  and section links on other pages point at `index.html#section`.
* At the end the log prints a link check, for example
  `Link check: 5 page(s) checked; every link opens a page or section`, or lists any link that still goes nowhere.
* Run it on an existing site with the **Add linked pages** button. A page the AI could not write keeps its `#`
  link and the log says why; press the button again to retry.

## 2. Contacts (email and social media)

Tick **Add contacts** in the Studio form and the email and social media boxes appear. Type a handle (`@mybakery`)
or paste the full link. When you build, they are added to the footer of every page by code, exactly as typed,
so the AI never rewrites them.

For a site that already exists, fill in those boxes at the top of the page, then press **Add contacts** next to the
site in "Previous sites". Pressing it again replaces the earlier contact details. **Remove contacts** takes them out.
Contacts never change the shop.

## 3. Shopping cart

Tick **Add shopping cart** before building, or press **Add shopping cart** next to an existing site. It adds
`shop.html`, `cart.html`, `cart.js`, `shop.css`, "Shop" and "Cart (n)" links on every page, and your item list:

Press **Edit shop items** next to the site and fill in the boxes for each item: **Item name**, **Item price**, and
**Choose picture** to upload a picture from your computer (JPEG, PNG, WebP or GIF; it is shrunk to a web-friendly size
and saved in the site's `assets/shop/` folder). Description, weight (used for USPS prices) and "In stock" are optional.
Use **+ Add an item** for more, then **Save and update the shop**. A mistake, such as a price that is not a number, is
marked in red on that row.

The shop starts with no items, so it stays empty until you add some. An item needs a name and a price before it can be
put in a cart.

The list is kept in `~/groq-website-studio/projects/<site-folder>/items.csv`, which you can also edit by hand:

```
id,name,price,description,image,weight_oz,in_stock
tee-black,Black T-Shirt,24.00,Soft cotton tee.,assets/shop/tee.jpg,8,yes
```

After editing the file by hand, press **Add shopping cart** next to the site again (or run
`python shop_cart.py projects/<site-folder>`) to refresh the pages. **Remove shop** takes the cart pages out and keeps
`items.csv`. The shop never changes the contacts.

## 4. Stripe payments

1. In the Studio open **Payments and shipping**, paste your Stripe *secret* key (start with a **test** key,
   `sk_test_...`), press **Save keys**. (Or run `python plugin_stripe.py setup`.)
2. Run the shop: `python shop_server.py projects/<site-folder>` and open http://127.0.0.1:8000/cart.html
   (previewing from the Studio works too). Test card: `4242 4242 4242 4242`, any future date, any CVC.

The cart page always has a **Checkout** button. It is greyed out, with the note "Online payment is not set up on this site yet.", until a key is saved and the page is opened through the Studio or `shop_server.py`; then it reads "Checkout with Stripe". You do not need to create products in Stripe: the name and price from your item list are sent to Stripe at checkout. Prices are re-read from `items.csv` on the server at
payment time. Not included: order emails, stock counts, tax setup, webhooks. Your Stripe Dashboard lists every payment.

## 5. USPS shipping prices

The old USPS Web Tools service (and its User ID) was shut down on January 25, 2026. Create an app at
https://developers.usps.com and use its Consumer Key and Consumer Secret (with the Prices and Tracking products)
as the Client ID and Client secret in **Payments and shipping**, plus the ZIP code you ship from.
The cart then asks for the shopper's ZIP, shows USPS Ground Advantage and Priority Mail prices, and that price is
added to the Stripe checkout. `weight_oz` in items.csv sets each item's weight (4 oz packaging is added).
Package tracking is available through `plugin_usps.track()` and the `api/track` route; there is no tracking page yet.

Optional settings (add with `python secrets_env.py set NAME`): `USPS_API_BASE=https://apis-tem.usps.com` (USPS test
server), `USPS_PACKAGE_INCHES=12x9x4`, `SHOP_COLLECT_SHIPPING=no`, `SHOP_PUBLIC_URL=https://your-domain`.

## Where the keys live and how they are protected

* Saved to `~/groq-website-studio/.env` (created if missing, set to owner-only permissions, added to `.gitignore`),
  next to your other settings. Existing lines in `.env` are kept.
* Only Python code on the server reads them. They are never put in any HTML or JavaScript file, never sent to
  the browser, and the Studio only shows the last 4 characters. Error messages and logs have keys removed.
* They are **not** base64-encoded or "encrypted": the program must read them back to use them, so any encoding
  would only look like protection. Anyone who can open files in your user account can read `.env`.
  Use a Stripe *restricted* key if you want to limit what it can do.
* The Studio refuses requests that do not come from `127.0.0.1` or that are not JSON, so another website open in your
  browser cannot change your saved keys. It never serves dot-files such as `.env`.

## Going public

`shop_server.py` runs a shop on your own computer. To take real payments from the internet you need a host that can
run Python, HTTPS in front of it, and `SHOP_PUBLIC_URL=https://your-domain` in `.env`. A plain static host
(for example GitHub Pages) can show the cart but cannot run Stripe or USPS. Use test keys until that is done.

## What was tested

Tested here with a fake AI model and fake Stripe/USPS servers: the page expander, contact/social blocks (also
running twice, and removal), the cart in a real headless browser, the API's rejection of tampered carts, key saving
and masking, and the Studio's form and routes. **Not tested:** real Groq output, a real Stripe or USPS account.
The USPS price response is read leniently because I could not check it against the live service.
