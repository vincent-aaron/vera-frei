"""python -m vera                      run the Discord bot
python -m vera find [query]         search from the terminal (prints JSON)
python -m vera prefs                show preferences (JSON)
python -m vera set-pref KEY JSON    e.g. set-pref positions '["Web Developer"]'
python -m vera applications         list applications (JSON)
python -m vera stats                statistics (JSON)
python -m vera ask "question"       chat with Vera
python -m vera background           run the bot with no console (used by the scheduled task)
python -m vera rounds            one test round now: fills forms, sends nothing (real rounds start from Discord after you approve a plan)
python -m vera answer "Q" "A"       save your answer to a question a round couldn't answer
python -m vera models              check every free AI provider: key set? does each model answer?

The CLI acts as the owner (DISCORD_OWNER_ID) unless VERA_USER=<discord id> is set.
It talks to the same service layer as the bot, so tools like Hermes Agent can
drive Vera with shell commands (see integrations/hermes/SKILL.md).
"""

import asyncio
import json
import os
import sys

from vera import config
from vera.services import Vera


def main(argv):
    cmd = argv[0] if argv else "run"
    if cmd == "run":
        from vera.bot.main import run
        return run()
    if cmd == "background":
        from vera.bot.main import background
        return background()
    vera = Vera()
    uid = int(os.getenv("VERA_USER") or config.OWNER_ID or 0)
    me = vera.user(uid)
    out = None
    if cmd == "find":
        res = asyncio.run(me.find_jobs(" ".join(argv[1:]) or None))
        out = {"sources": res["status"], "total_matches": res["total_matches"],
               "picks": [{"key": vera.job_key(j["id"]), "title": j["title"], "company": j["company"], "url": j["url"],
                          "score": ev["score"], "missing": ev["missing"]} for j, ev in res["picks"]]}
        for j, _ in res["picks"]:
            me.set_job_state(j["id"], "posted")
    elif cmd == "prefs":
        out = me.prefs
    elif cmd == "set-pref" and len(argv) >= 3:
        out = me.update_prefs({argv[1]: json.loads(" ".join(argv[2:]))})
    elif cmd == "applications":
        out = vera.db.applications(uid, limit=100)
    elif cmd == "stats":
        out = me.statistics()
    elif cmd == "rounds":
        from vera.agents import rounds
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        print(rounds.report(asyncio.run(rounds.run_once(vera, uid, dry_run=True))))
        return 0
    elif cmd == "models":
        return check_models()
    elif cmd == "answer" and len(argv) >= 3:
        from vera.agents import rounds
        rounds.save_answer(uid, argv[1], " ".join(argv[2:]))
        out = {"saved": argv[1]}
    elif cmd == "ask" and len(argv) >= 2:
        from vera.agents import assistant
        r = asyncio.run(assistant.handle(me, " ".join(argv[1:])))
        out = {"reply": r.get("reply"), "changes": r["changes"], "action": r["action"]}
    else:
        print(__doc__)
        return 1
    print(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    return 0


def check_models() -> int:
    """One short call per model, so you can see which free providers work. Keys are never printed."""
    import httpx
    from vera import free_models as FM
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ready = {p["name"]: p for p in FM.usable()}
    ENV_FILE = str(config.ROOT.parent / ".env")
    for p in FM.PROVIDERS:
        if p["name"] not in ready:
            print(f"-  {p['name']:<12} not set up: add {p['key']} to {ENV_FILE}  ({p['get_key']})  {p['notes']}")
            continue
        r = ready[p["name"]]
        for model in r["models"]:
            try:
                res = httpx.post(f"{r['base']}/chat/completions", headers=r["headers"], timeout=60, json={
                    "model": model, "max_tokens": 400, "messages": [{"role": "system", "content": FM.ENGLISH},
                                                                  {"role": "user", "content": "Reply with just: OK"}]})
                ok = res.status_code == 200 and (res.json().get("choices") or [{}])[0].get("message", {}).get("content")
                print(f"{'OK' if ok else 'X '} {p['name']:<12} {model:<45} {'' if ok else 'HTTP ' + str(res.status_code)}")
            except httpx.HTTPError as e:
                print(f"X  {p['name']:<12} {model:<45} {type(e).__name__}")
    print("OpenRouter (free models only):", "key set" if config.OPENROUTER_API_KEY else "no key")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]) or 0)
