# wordlist.txt.gz

English words for the page quality score (`efp/documents/quality.py`).

- Source: `words_alpha.txt` from <https://github.com/dwyl/english-words> (The Unlicense, public domain),
  fetched 2026-11-22, SHA-256 `3ed0c94610d8bcf7c11bbb49c56aa49c7234d32b66824df91f554169e572da48`.
- Built by: lower-casing, keeping 3 to 24 letters, adding `wordlist_legal.txt`, sorting, gzip.
- Used only as a yes/no membership test, never to correct text.
- The word list is pinned with the other instruments (`efp/instruments.py`); a change is a score-version change.
