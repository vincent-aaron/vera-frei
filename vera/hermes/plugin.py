"""register(ctx): Vera's tools, hooks and Discord wiring, for the Hermes plugin in profiles\\vera\\plugins\\vera."""

import logging

from vera.hermes import hooks, runtime, send_guard, tools

log = logging.getLogger("vera.hermes")


def register(ctx) -> None:
    for fn, schema in tools.TOOLS:
        ctx.register_tool(name=schema["name"], toolset=tools.TOOLSET, schema=schema, handler=tools.wrap(fn),
                          description=schema["description"].split("\n")[0], emoji="💼")
    ctx.register_hook("pre_gateway_dispatch", hooks.on_inbound)
    ctx.register_hook("pre_llm_call", hooks.before_turn)

    def wire(bot, adapter):
        try:
            send_guard.install()          # no double replies when the network drops mid-send
        except Exception:
            log.exception("couldn't install the duplicate-reply guard")
        try:
            runtime.connect(bot)
        except Exception:
            log.exception("couldn't wire Vera into Discord")

    ctx.register_platform_handler("discord", wire)
    log.info(f"Vera registered {len(tools.TOOLS)} tools")
