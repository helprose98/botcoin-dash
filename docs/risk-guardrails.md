# Risk Guardrails

This project touches a live-money system.

Core rules:

- Cold storage and exchange balance are different risk tiers.
- Cold storage is the long-term hold stack and is not the same as the bot's playable balance.
- The bot may fully control only the designated playable exchange balance.
- Changes affecting selling, re-entry, thresholds, or automation scope must be explained before implementation.
- Any change that could increase downside risk must be explicitly called out.
- Start read-only on external systems whenever possible.
- Never place secrets, API keys, tokens, or other sensitive credentials into chat, docs, commits, or code comments.
- Treat any push to an auto-deploy branch or other production-connected branch as a production event and verify carefully before shipping.
