# Student-Facing Red Lines — Serving Real Students

This system answers real UQ students making real decisions (enrolment,
withdrawal, prerequisites, fees, deadlines). A wrong answer can cost a student
money or a deadline. These red lines hold for every feature and every new data
source. They are not optional.

## 1. High-cost answers come from deterministic data, never free LLM generation

High-risk topics: prerequisites, census date, fees, withdrawal impact, exam
dates, any deadline.

- For these, do NOT let the LLM recall + paraphrase freely. Answer from
  structured data with exact lookup.
- Example: prerequisites already exist as structured course data — return them
  via SQL exact match + the course-profile link, not vector recall fed to the
  LLM.
- The LLM only classifies, drafts wording, or resolves ambiguity. It never
  decides a high-risk fact. (Same as the `code-style` / global rule 12:
  deterministic logic lives in code.)

## 2. Every answer carries the official source URL

- The product is "help you find and understand official info", not "I am the
  authority". Position it so a student can verify in one click.
- Policy answers also carry the PPL number + section (plan §5).
- If the answer links back to the official page, an error is recoverable: the
  student catches it. An answer with no source is the dangerous one.

## 3. Refuse over wrong

- When retrieval is weak or the question is high-risk and unmatched, say "I'm
  not sure" + give the official link. Do not produce a confident guess.
- A "no answer + link" outcome is a success here, not a failure.

## 4. Data freshness is the lifeline

- Policies, fees, and offerings change every semester. Stale data = wrong
  answer, even if retrieval and generation are perfect.
- The incremental refresh (plan §6) must actually run, not just exist. Run it
  more often around semester start (Feb / Jul).
- Carry the fetch time / sitemap `lastmod` with the answer. If the page is old,
  tell the student to confirm on the official site.

## 5. Scope narrow before going broad

- Make ONE scenario trustworthy first: course / scheduling / prerequisites —
  the area where we are strongest (structured data + the simulator) and UQ's own
  search is weakest. FAQ / policy content is the supporting layer.
- One scenario answered reliably beats full coverage answered half-confidently.

## 6. Accuracy eval before serving real students

- Before any feature reaches students, run a fixed set of real high-risk
  questions and human-check the answers for correctness — not just "the system
  did not crash" (global rule 16).
- Weak tests that only assert "returns something" do not count as passing.

## 7. Crawl authorization and data use

- `support.my.uq.edu.au` is crawled under explicit owner authorization, for
  private / non-commercial use only (its robots.txt disallows generic bots —
  Oracle Service Cloud pricing). See `ROBOTS_OVERRIDE` in
  `app/scrapers/kb_discover.py`.
- Do not republish raw scraped text publicly. Answers link back to the official
  page; the crawl is an index, not a rehost.
- Any new domain added to the crawl must clear the same check: is crawling it
  authorized, and is the content allowed to be served this way?
