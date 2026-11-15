# Contributing

Thanks for helping! The most valuable contribution is simple: **tell us your ONT works.**

## 📟 Report a device (2 minutes)

Open an issue titled `Works: <model>` (or `Broken: <model>`) with:

1. The ONT model (on the sticker), e.g. `DG-GR6011`
2. Firmware version (from `show status`)
3. The output of `ont-mcp --check`. **Remove your IP and serials if you like.**
4. If something failed: what you asked, and the error

If your ONT's prompt isn't `>` or `#`, tell us what it looks like. You can already work around it with `ONT_PROMPT`.

## 🛠️ Code

- Keep it **one file, standard library only.** That's a feature, not a limitation.
- Every change to the safety logic (`classify`, `ask_consent`, `execute`) needs a test.
- Run the tests before you open a PR:

```bash
python3 -m unittest -v
```

- Never commit real hosts, passwords or config files. `.gitignore` already covers `ont.json` and `*.password`.

## 🔒 Security issues

Please don't open a public issue for a way to bypass the approval dialog. Contact the maintainer privately first.
