# Authorized prompt sent without a token (defect)

The single authorized prompt was sent with an empty token. `run/token.txt` was never written, because the earlier denied command contained that write. The variable `$TOKEN` expanded to empty, so the prompt read: "Remember this test token exactly for later: . Then reply with only the word OK."

Result: `agent prompt` exit 0, status `idle`, session `79cab582-a6fc-4303-8424-469d60da4ac4` (`10-turn1.json`).

Consequence: no unique token exists in the conversation. Recall after restart cannot prove continuity of a token under the one-prompt authorization. A second prompt needs separate authorization.
