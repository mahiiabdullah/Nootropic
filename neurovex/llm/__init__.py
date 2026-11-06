"""Neurovex LLM layer: many providers behind one router with failover.

    from neurovex.llm.router import LLMRouter
    router = LLMRouter.from_settings(settings, recorder)
    value, result = await router.complete_json(request, parse=MyModel.model_validate_json)

Kept import-free so `neurovex.core.config` can import the provider catalog.
"""
