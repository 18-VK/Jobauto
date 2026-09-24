# Adding a portal

Any job site with a search-results page can be added from the dashboard. No
code. The only part that takes care is the selectors, and this walks through
getting them right.

Time: about ten minutes for a site you can already sign in to.

---

## The short way (most sites)

1. On the site, in your normal browser, run a search for **your first
   configured role in your first configured city** (see Preferences). Copy the
   address bar.
2. Dashboard -> **Preferences -> Add a portal**: type a name, paste the
   address, **Add portal**.
3. Your PC looks at that page on its next poll (within a minute if it is
   online) and the Portals list updates with what it found:

   - **found 24 jobs** -- done. It also shows *search page: ok* and
     *login page: ok* if it could reach the site's sign-in page.
   - **needs sign-in** -- the site wants an account before it shows results.
     On your PC run `jobauto login --portal <id>`, sign in, then press
     **Try again** in the list.
   - **no job list found** -- the address was not a results page. Search on
     the site first, then paste the address you land on.

That is the whole thing. The steps below are the long way, for a site the
automatic look cannot read -- and they explain what the automatic look does.

---

## 0. Before you start

- The site has a **search page** whose URL carries the query (for example
  `.../search?q=qa+automation&location=Delhi`). If the search only works by
  typing into a box with no URL change, it needs a Python adapter -- stop here
  and ask.
- You can sign in to it in a normal browser.
- Your PC has the current agent (`setup-agent.bat`, re-run any time).

---

## 1. Find the search URL pattern

In your normal browser, search the site for a role and a city. Copy the
address bar. Replace the parts you typed with tokens:

| You typed | Replace with |
|---|---|
| the role, e.g. `qa automation` | `{keywords}` (URL-encoded, with your include keywords) or `{role}` (plain) |
| the city, e.g. `Delhi` | `{location}` (URL-encoded) |
| a role in a path, e.g. `/qa-automation-jobs` | `{role_slug}` |
| a city in a path, e.g. `-in-delhi-ncr` | `{location_slug}` |
| years of experience | `{exp}` |
| "posted in the last N days" | `{days}` or `{days_bucket}` (snaps to 1/3/7/15/30) |
| page number | `{page}` |

Example:

```
https://www.foundit.in/srp/results?query={keywords}&locations={location}
```

Unknown tokens become empty, so only use the ones the site supports.

---

## 2. Find the selectors

This is the part that has to match the page **signed in, after it has
finished loading** -- every portal draws its results in JavaScript, so what
you see in "view source" is not what the scraper sees.

In your normal browser, on the results page:

1. Right-click a job's **title** and choose *Inspect*.
2. In the element panel, walk **up** the tree until you reach the element
   that wraps one whole job -- title, company, location together. Its class
   is your **result card** selector, e.g. `.cardContainer` or
   `div.job-tuple`. Check that the same class repeats once per job.
3. Back inside that card, note the class of the **title**, the `<a>` that
   is the **link** to the job, the **company** and the **location**.

Rules of thumb:

- Prefer classes that describe the thing (`.jobTitle`, `.companyName`) over
  generated ones (`.css-1x2y3z`, `.sc-bdfBwQ`) -- generated ones change on
  every deploy.
- A selector is matched **inside the card**, so `.title` means "the `.title`
  within this job", not the page's.
- The link selector must point at an `<a>`; its `href` is taken.

If the site has an obvious sign-in indicator (avatar, "My profile" link),
copy its selector too -- that is the **signed-in marker**. Optional; the
scraper falls back to checking it is not on a login page.

---

## 3. Add it in the dashboard

**Preferences -> Add a portal.** Fill in:

| Field | Example |
|---|---|
| Id | `foundit` -- lowercase, no spaces; this is the name you will type in commands |
| Name | `Foundit` |
| Site | `https://www.foundit.in` |
| Login page | `https://www.foundit.in/login` |
| Search URL | the pattern from step 1 |
| One result card | from step 2 |
| Title / Link / Company / Location | from step 2 |
| Pages to read | `3` is plenty |

Everything else is optional. **Save portal.** It appears in the Portals list
above with a `custom` tag and its own on/off switch. If the save is refused,
the message says which field is wrong.

Your PC picks it up within a minute (or when it next comes online).

---

## 4. Sign in, once

On your PC:

```powershell
jobauto login --portal foundit
```

A real browser opens on the login page. Sign in by hand, including any OTP.
Close the window when done. Nothing is stored except the session, the same
way your own browser keeps you signed in.

---

## 5. Prove the selectors match

Do this before trusting it:

```powershell
jobauto dump --portal foundit
```

It opens the search page in the signed-in browser, runs exactly the scrape a
real run does, and prints:

```
  Foundit
    opening https://www.foundit.in/srp/results?query=...
    landed  https://www.foundit.in/srp/results?query=...
    cards   24 matched search.result_card
    saved   ...\data\debug\foundit.html
```

- **cards N** with N > 0 -- done. Go to step 6.
- **cards 0** -- the line after it names the cause. The three you will see:
  - *the site sent us to .../login* -- step 4 did not stick; sign in again.
  - *a bot check, not a results page* -- the site blocks automation; this
    portal will not work, remove it.
  - *nothing matched search.result_card* -- the selector is wrong. Open the
    saved `.html` in a browser, Inspect a job there, and fix the selector in
    the dashboard (Portals -> Edit). Repeat this step.

---

## 6. Run it

```powershell
jobauto discover --portal foundit
jobauto shortlist --why
```

From now on it is part of every scheduled run. Switch it off any time from
Portals; remove it with the Remove button (your saved login on the PC is
kept, so adding it back later needs no new sign-in).

---

## What it cannot do

- **Apply automatically.** A custom portal is search-only unless you fill in
  the *Apply button* selector, and even then only the simplest one-click
  "apply" works. Anything with a form, a wizard or a chatbot needs a Python
  adapter. Jobs from it still appear in your shortlist and you apply on the
  site.
- **Beat a bot check.** If `dump` reports one, that is the answer.
- **Replace a shipped portal.** An id like `naukri` is refused.
