from llmcheck.pipeline import LlmCheckError, LlmCheckSettings, process_documents

__all__ = [
    "LlmCheckError",
    "LlmCheckSettings",
    "process_documents",
    "agent_api",
    "list_profiles",
    "submit_convert",
    "get_job",
    "get_final_markdown",
    "build_settings_from_env_and_args",
    "SCHEMA_VERSION",
]


def __getattr__(name: str):
    if name in {
        "agent_api",
        "list_profiles",
        "submit_convert",
        "get_job",
        "get_final_markdown",
        "build_settings_from_env_and_args",
        "SCHEMA_VERSION",
    }:
        import importlib

        _agent_api = importlib.import_module("llmcheck.agent_api")
        if name == "agent_api":
            return _agent_api
        return getattr(_agent_api, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
