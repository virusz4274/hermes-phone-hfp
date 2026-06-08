from .hooks import pre_llm_call_hook


def register(ctx) -> None:
    ctx.register_hook("pre_llm_call", pre_llm_call_hook)
