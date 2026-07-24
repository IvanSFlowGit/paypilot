# Copy-enforcement runbook

What to do after `scripts/canary.py` reports a hit.
The canary finds the copy; the licence decides what you may do about it; this file is the procedure that connects the two.

PayPilot is licensed under PolyForm Noncommercial 1.0.0 (see `LICENSE` and `NOTICE`).
Noncommercial use is permitted with attribution.
Any commercial use requires a separate written licence from the author (Ivan S, github.com/IvanSFlowGit).
A copy that strips the copyright header, or is used commercially without a licence, is the infringement this runbook addresses.

## 0. Read the findings, not the email

The alert email never contains repo names, paths, or links: those are written by whoever published the copy, so quoting them in an inbox would let them choose what reaches you.
The full hit list is in the local file the scan writes (`canary-findings.json` by default, or `CANARY_REPORT_PATH`).
Open it from a trusted terminal.
It is json-escaped, so a hostile repo name cannot run in your shell, but still treat every string in it as untrusted: do not paste a URL from it blindly, and do not run anything it contains.

## 1. Classify the hit

- CANARY hit (`pp-824b8f4a0fd0`): this is a copy, not a coincidence. The string exists nowhere but this project. Proceed.
- PHRASE hit: a lead, not proof. Open the file, read the surrounding code, and confirm it is genuinely our source and not a common idiom before proceeding.

Stop here if it is a false positive. Record why, so the next run of the same hit is quick to dismiss.

## 2. Preserve evidence before you act

Do this first, because an infringer who is contacted often deletes.

1. Capture the page: full-page screenshot of the repo/file, with the URL bar and date visible.
2. Snapshot it independently: submit the URL to `https://web.archive.org/save/` and to `https://archive.today/`, and record the resulting permanent links.
3. Clone at a fixed point: `git clone` the copy, note the exact commit SHA and its author/date, and confirm the canary or copied lines are present in that commit. Keep the clone offline; do not build or run it.
4. Identify the publisher: GitHub account, linked website, company name, and (for a commercial site) the operating entity from its footer, terms, or WHOIS.
5. Note whether the copyright header and `NOTICE` were removed: removal of attribution is a separate, aggravating act.

Store all of this in a dated folder outside the repo. This is the evidence bundle every later step references.

## 3. Decide the remedy by how it is used

- Noncommercial use, attribution intact: lowest priority. Optionally send a courtesy note confirming the licence terms. No further action required.
- Noncommercial use, attribution stripped: send the takedown/restore-attribution notice (Section 4). The licence was breached even though no money changed hands.
- Commercial use, no licence: the main case. You have two levers, not one: demand takedown (Section 4), or convert them to a paying licensee (Section 5). Choose per infringer; a competitor gets takedown, a clumsy startup may be worth a licence.

## 4. Takedown

For a GitHub-hosted copy, GitHub honours a DMCA notice for a verbatim code copy.

1. Follow `https://docs.github.com/site-policy/content-removal-policies/dmca-takedown-policy` and file at `https://support.github.com/contact/dmca-takedown`.
2. The notice needs: identification of the copyrighted work (this repo, with a permalink to a copied file), identification of the infringing material (the copy's permalink from your evidence bundle), your contact details, and the two required statements (good-faith belief, and accuracy under penalty of perjury).
3. GitHub forwards the notice to the infringer and typically publishes it in `github.com/github/dmca`. Expect the copy to be removed or the infringer to file a counter-notice within about 10 business days.

For a copy hosted elsewhere: send the same notice to the host (from WHOIS / the site's abuse contact) and, if it is indexed, to Google via `https://reportcontent.google.com/forms/dmca_search`.

Before filing, confirm the material really is a copy of your work and that no licence covers it: a bad-faith DMCA notice carries its own liability.

## 5. Cease-and-desist or licence offer (commercial infringer)

Where takedown alone under-serves the goal (a commercial operator who should pay), send a single, factual letter:

- State the work, the licence (PolyForm Noncommercial 1.0.0), and that commercial use requires a separate licence.
- State the evidence you hold (dates, commit SHA, archived links) without attaching the full bundle.
- Offer the fork: obtain a commercial licence at a stated fee within a stated window (e.g. 14 days), or remove the material and confirm removal in writing.
- Keep it professional and unemotional. This letter may be read by a court.
- Do not threaten anything you will not do, and do not make public accusations before a response window closes.

Route anything past a first letter, or any infringer who pushes back, through a solicitor before escalating.

## 6. Escalate

If the infringer ignores both the takedown and the letter, and the commercial harm justifies cost:

- Instruct an IP solicitor in the relevant jurisdiction (the infringer's or the host's).
- Governing law follows the licence and your entity; confirm it with the solicitor rather than assuming.
- Damages for wilful commercial infringement, and the removed-attribution point, are the solicitor's to weigh.

Escalation is a business decision on cost versus harm, not an automatic next step.

## Do not

- Do not access, probe, or attempt to log into the infringer's systems: finding a copy authorises no intrusion, and probing may itself be unlawful.
- Do not publicly name or accuse the infringer before you have the evidence bundle and, for anything beyond a takedown, legal advice.
- Do not process more personal data about the infringer than the enforcement needs, and hold what you do keep under the same data-protection rules as everything else.
- Do not rotate the canary as a reaction to a single hit: rotating it blinds you to every copy already in the wild. Rotate only on a deliberate re-baseline (change it in `data/templates/dunning.json` and `scripts/canary.py` together, re-plant, then re-run).

## The honest limit

This catches the lazy copier who keeps the fingerprints, which is the common case.
It does not catch a competent one who strips them; nothing does.
The canary lowers the cost of finding the copies you can find, and this runbook turns a find into an outcome.
