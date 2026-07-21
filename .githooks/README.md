# .githooks

Client-side git hooks wired via `git config core.hooksPath .githooks`.

Invariants enforced:
- `pre-commit`: author and committer email must equal the configured
  target email.
- `commit-msg`: rejects any reference to banned domains (case-
  insensitive), and rejects `Co-authored-by:` trailers whose email
  is not the target or in the explicit allow-list.

These run locally only — they do not protect GitHub's server. Server-
side branch protection / push rules are required for defense beyond
this developer's machine.

Installation:
1. Substitute the target email, banned-domain regex, and
   allowed co-author list with the literal values for your repo. (Done here.)
2. `chmod 755 .githooks/pre-commit .githooks/commit-msg`.
3. `git config --local core.hooksPath .githooks`.
4. Commit the `.githooks/` directory. The pre-commit hook will
   self-validate the commit.

Meta-commit hazard: the commit introducing these hooks must not
literally spell out a banned domain in its message — the
commit-msg hook would reject it. Use generic wording like "the
retired personal domains".
