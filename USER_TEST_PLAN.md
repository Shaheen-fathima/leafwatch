# Problem 6: low farmer participation. What code can and cannot show

**What the software now does (verified by automated tests, see tests/test_participation.py):**
verification requests are shown to the nearest farmers (inside 1.5 km, otherwise inside 5 km); a photo (healthy or sick) answers the request
whether sent from the request button or the normal Report tab; answering pays +15 points once; an unanswered request gets exactly one
reminder after 3 h (page toast if the page is open, flag + text on the Alerts tab) and expires after 24 h; the Admin screen and `/api/stats`
report `verify_response_rate`, `verify_median_answer_min` and expired requests.

**What it cannot show:** whether real farmers take part. That needs a field trial. Suggested minimum:

1. 15 to 30 farmers (or 3 villages x 5 farmers), 2 weeks, their own phones, Tamil UI text checked by a native speaker.
2. Seed one real, agronomist-confirmed case per week; ask neighbours to verify; record for every request: asked, reminded, answered (Y/N), minutes to answer, photo accepted (Y/N).
3. Read the numbers from `/api/stats` (response rate, median minutes) and `python check_integrity.py` (data sanity); do short interviews on why people did not answer.
4. Decide targets BEFORE the trial (e.g. response rate >= 40 % within 24 h; median < 6 h). Compare points/badges against a group without them if you can.
5. Reminders only reach people with the page open. For offline phones you need SMS/WhatsApp/push (not implemented, see HOSTING.md).
