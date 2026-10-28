"""
Settings, read from environment variables and the .env file.

Each setting is one line in ``Config``: ``NAME = setting(parse, default)``,
read from the environment variable NAME. ``SETTINGS`` holds them all, so
/admin can list every one (secrets as set or not), ``Config.apply`` can
re-read any, and a test checks env.example mentions each.
"""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, TypeVar, cast

import envfile

# Settings file, read before anything else here. A non-empty environment
# variable wins over it. The setup wizard (/admin/setup) writes to it.
ENV_FILE = Path(os.getenv("ENV_FILE") or Path(__file__).resolve().parent / ".env")
# Settings that came from the file, as opposed to the real environment.
_FROM_FILE = set(envfile.load(ENV_FILE))
_HERE = Path(__file__).resolve().parent

T = TypeVar("T")


# How a setting's text becomes its value.
def flag(text: str) -> bool:
    return text.strip().lower() in ("1", "true", "yes", "on")


def items(text: str) -> List[str]:
    """Comma-separated values; empty is none."""
    return [v.strip() for v in text.split(",") if v.strip()]


def optional(text: str) -> Optional[str]:
    """Text, with empty meaning unset (keys and secrets)."""
    return text or None


def directory(text: str) -> Path:
    """A path, made absolute against the working directory."""
    return Path(text).resolve()


@dataclass
class Setting:
    parse: Callable[[str], Any]
    # The value when the variable is unset: text is parsed like the variable's, a callable is called.
    default: Any
    secret: bool = False
    # The least a number may be; None leaves it unchecked.
    minimum: Optional[float] = None
    name: str = ""

    def read(self) -> Any:
        """The value, or a ValueError naming the variable and what is wrong with it (at startup, or when
        ``Config.apply`` re-reads it): not a bare parse error at first use."""
        raw = os.environ.get(self.name)
        # An empty number counts as unset (compose passes "${MAX_MODELS:-}"); an empty path or list means off.
        if raw is None or (raw == "" and self.parse in (int, float)):
            default = self.default() if callable(self.default) else self.default
            return self.parse(default) if isinstance(default, str) else default
        try:
            value = self.parse(raw)
        except (TypeError, ValueError) as e:
            shown = "(hidden)" if self.secret else repr(raw)
            raise ValueError(f"{self.name}={shown} is not a valid value: {e}") from None
        if self.minimum is not None and value < self.minimum:
            raise ValueError(f"{self.name}={raw!r} is below the least allowed, {self.minimum:g}")
        return value


SETTINGS: Dict[str, Setting] = {}


def setting(parse: Callable[[str], T], default: Any, secret: bool = False, minimum: Optional[float] = None) -> T:
    """Declare a setting; ``_from_environment`` replaces it with its value. Typed as the value for editors."""
    return cast(T, Setting(parse, default, secret, minimum))


def _from_environment(cls: type) -> type:
    for name, value in list(vars(cls).items()):
        if isinstance(value, Setting):
            value.name = name
            SETTINGS[name] = value
            setattr(cls, name, value.read())
    return cls


@_from_environment
class Config:
    ENV_FILE = ENV_FILE
    # Send /admin to the setup wizard while no API key is configured.
    SETUP_WIZARD = setting(flag, True)
    # The one-time code the wizard asks for; a random one is logged at startup when unset.
    SETUP_CODE = setting(optional, None, secret=True)

    # Model cache settings. MAX_MEMORY_GB budgets the loaded models' sizes
    # (estimated before loading); 0 means no budget, only MAX_MODELS.
    MAX_MODELS = setting(int, 3, minimum=1)
    MAX_MEMORY_GB = setting(float, 8.0, minimum=0)
    # Seconds a request may wait for its model, for room to load it or behind the requests ahead of it, before a 503.
    MODEL_WAIT_TIMEOUT = setting(float, 120.0, minimum=0)
    # Unload a model nobody used for this many seconds; 0 keeps it until evicted.
    MODEL_IDLE_TIMEOUT = setting(float, 0.0, minimum=0)
    # Seconds an idle model keeps its place before another model may take it: its client's next request is usually
    # on the way, and swapping models on every request costs a load each time. 0 lets an idle model go at once.
    MODEL_KEEP_S = setting(float, 3.0, minimum=0)
    # Seconds a load waits before it may take a busy model's place from work of the same priority (X-Priority): the
    # busy model's requests finish, new ones wait, and it is unloaded. Keep it well above a load time and below
    # MODEL_WAIT_TIMEOUT. 0: a busy model keeps its place from work of the same priority.
    MODEL_TURN_S = setting(float, 15.0, minimum=0)
    # Models to load at startup, as "model_id:task" (or "model_id" for the
    # task a model file claims it for, else text-generation), comma-separated.
    # PINNED_MODELS are loaded too and never evicted; only an explicit unload
    # removes them.
    PRELOAD_MODELS = setting(items, "")
    PINNED_MODELS = setting(items, "")
    # Models served by OpenAI-compatible servers (Ollama, LM Studio, mlx-vlm,
    # vLLM, llama-server, ...), each with backends tried in order; see
    # model_servers.example.toml. Missing: none.
    MODEL_SERVERS_FILE = setting(lambda text: Path(text or _HERE / "model_servers.toml"), _HERE / "model_servers.toml")
    # Model servers edited in /admin (JSON); each replaces the file's entry of
    # the same name. Empty: /admin can show them but not change them.
    MODEL_SERVERS_SAVED_FILE = setting(lambda text: Path(text) if text else None, "data/model_servers.json")
    # Let /admin save model server commands (cmd, env), which run on this host
    # as this service's user. Off: /admin keeps only the ones the file gives.
    MODEL_SERVERS_ALLOW_COMMANDS = setting(flag, False)

    # API settings. This machine only unless told otherwise (the image sets
    # 0.0.0.0): until the setup wizard runs, the API has no key.
    HOST = setting(str, "127.0.0.1")
    PORT = setting(int, 8000, minimum=1)
    WORKERS = setting(int, 1, minimum=1)
    RELOAD = setting(flag, False)  # dev-only auto reload
    DEBUG = setting(flag, False)   # include exception text in error responses

    # Request limits
    MAX_INPUT_CHARS = setting(int, 20000, minimum=1)
    MAX_GENERATION_LENGTH = setting(int, 1024, minimum=1)
    # Images and audio in chat messages: per item, and items per request.
    MAX_MEDIA_BYTES = setting(int, 10 * 1024 * 1024, minimum=1)
    MAX_MEDIA_PARTS = setting(int, 8, minimum=0)
    # /v1/audio/transcriptions uploads (OpenAI's own limit is 25 MB).
    MAX_AUDIO_BYTES = setting(int, 25 * 1024 * 1024, minimum=1)
    # Texts per /v1/embeddings request.
    MAX_EMBEDDING_INPUTS = setting(int, 256, minimum=1)
    # Inputs per POST /inference/batch request.
    MAX_BATCH_INPUTS = setting(int, 256, minimum=1)
    # Milliseconds a single request to an idle model waits for others to share a forward pass with, on a model that
    # answers several in one call (the generic classifiers and question-answering models); requests waiting for a busy
    # model are batched without it. 0 answers each request alone.
    BATCH_WAIT_MS = setting(float, 2.0, minimum=0)
    # Token IDs a prompt given as tokens may hold, and prompts per /v1/completions request.
    MAX_PROMPT_TOKENS = setting(int, 32768, minimum=1)
    MAX_COMPLETION_PROMPTS = setting(int, 64, minimum=1)
    # Hub downloads (/hub/downloads) running at once; more wait in order.
    MAX_HUB_DOWNLOADS = setting(int, 2, minimum=1)

    # CORS settings
    CORS_ORIGINS = setting(items, "*")

    # Access control. When API_KEY is set, every endpoint except / and /health
    # requires "Authorization: Bearer <key>" or "X-API-Key: <key>".
    API_KEY = setting(optional, None, secret=True)
    # Model IDs clients may load. Empty means any Hugging Face Hub model.
    ALLOWED_MODELS = setting(items, "")
    # Key for administration: the /admin dashboard and its API, loading and
    # unloading models, and /models/status. Unset means API_KEY guards those
    # too. The admin key is also accepted wherever API_KEY is.
    ADMIN_API_KEY = setting(optional, None, secret=True)

    # Model settings
    DEFAULT_DEVICE = setting(str, "auto")  # auto, cpu, cuda, mps
    # Hub repos allowed to run their own Python code (FastVLM needs this).
    # TRUST_REMOTE_CODE=false disables it for all of them.
    TRUST_REMOTE_CODE = setting(flag, True)
    TRUST_REMOTE_CODE_MODELS = setting(items, "apple/FastVLM-0.5B,apple/FastVLM-1.5B,apple/FastVLM-7B")

    # Directory that image paths given to /vision-language must live under.
    IMAGE_DIR = setting(directory, "data/images")

    # The embedded ArcadeDB database in this directory holds /collections, the
    # /db tables, traces and evals, the evidence ledger, jobs, request metrics
    # and the enrichers' result cache. Empty turns all of those off.
    ARCADEDB_PATH = setting(str, "data/arcadedb")
    # Most memory ArcadeDB's in-process JVM may use (it shares the process with the models).
    ARCADEDB_HEAP = setting(str, "1g")

    # Traces (evals/): each request's input and output, kept in the ArcadeDB
    # database for debugging and for building benchmark datasets. A day's
    # traces share a bucket, dropped whole once older than this many days;
    # 0 records none, and drops those kept before. Needs ARCADEDB_PATH.
    TRACE_RETENTION_DAYS = setting(float, 3.0, minimum=0)
    # Longest string a trace keeps per field; longer ones are cut.
    TRACE_MAX_CHARS = setting(int, 8000, minimum=0)
    # The LLM judge for benchmark runs (/admin Evals): a model this service
    # runs (its ID, asked through its own /v1), or one behind an OpenAI-compatible
    # endpoint when JUDGE_BASE_URL is set; the key is sent to that endpoint only.
    # Each judging can name another.
    JUDGE_MODEL = setting(str, "")
    JUDGE_BASE_URL = setting(str, "")
    JUDGE_API_KEY = setting(optional, None, secret=True)

    # Jobs (jobs/): long-running scripts such as AutoGluon training, each run
    # in its own process. JOBS_DIR holds each job's files (the job table is in
    # the ArcadeDB database); empty disables jobs.
    JOBS_DIR = setting(str, "data/jobs")
    # Input files jobs may read (uploaded with POST /jobs/data).
    JOB_DATA_DIR = setting(directory, "data/datasets")
    # Job environments: JOB_ENVS_DIR/<env>/bin/python, made by scripts/setup_job_env.sh.
    JOB_ENVS_DIR = setting(directory, "envs")
    # Jobs run at once; more wait in a queue.
    MAX_RUNNING_JOBS = setting(int, 1, minimum=1)
    # CPU cores a job may use; 0 is every core but one, which stays with the service.
    JOB_NUM_CPUS = setting(int, 0, minimum=0)
    # cpu, cuda, mps or auto. Jobs get their own process, so on an Apple GPU
    # they do not take turns with the models; cpu keeps them off it.
    JOB_DEVICE = setting(str, "cpu")
    # Webhooks: URLs POSTed to when a job or an eval run finishes, and the secret the
    # X-Toolkit-Signature header signs the body with (empty: no signature).
    WEBHOOK_URLS = setting(items, "")
    WEBHOOK_SECRET = setting(optional, None, secret=True)
    # Largest file POST /jobs/data accepts.
    JOB_MAX_UPLOAD_BYTES = setting(int, 2 * 1024 ** 3, minimum=1)

    # Enrichers (enrichers/): look a value up in outside services. ENRICHERS_DIR
    # holds the operator's own enrichers (one file each, written by /admin);
    # empty turns enrichers off.
    ENRICHERS_DIR = setting(str, "data/enrichers")
    # Which sources may run, by what their terms allow: open, attribution,
    # non_commercial, paid. A commercial deployment drops non_commercial.
    ENRICHER_TERMS = setting(items, "open,attribution,non_commercial,paid")
    # Let enrichers call private and local addresses (a source on the LAN).
    ENRICH_ALLOW_PRIVATE = setting(flag, False)
    # Enrichers running at once across all lookups.
    ENRICH_CONCURRENCY = setting(int, 8, minimum=1)

    # Evidence capture (evidence/): signed, hash-chained records of web pages a
    # browser extension captures, with their files. EVIDENCE_DIR holds the
    # capture directories and the signing key; empty turns it off. The ledger
    # itself is in the ArcadeDB database, so ARCADEDB_PATH is needed too.
    EVIDENCE_DIR = setting(str, "data/evidence")
    # RFC 3161 time stamping authority each record's hash is sent to; empty disables it.
    EVIDENCE_TSA_URL = setting(str.strip, "https://freetsa.org/tsr")
    # Ed25519 private key (PKCS#8 PEM), made on first use; its public key is written beside it.
    EVIDENCE_KEY_FILE = setting(
        lambda text: Path(text) if text else Path(os.getenv("EVIDENCE_DIR") or "data/evidence") / "keys" / "ed25519.pem",
        lambda: Path(os.getenv("EVIDENCE_DIR") or "data/evidence") / "keys" / "ed25519.pem",
    )
    # Browser origins allowed on /evidence, exactly; empty means any chrome-extension:// or moz-extension://.
    EVIDENCE_ALLOWED_ORIGINS = setting(items, "")
    # Look the captured page's address and hosts up with the enrichers and keep what they
    # said inside the signed record, within this many seconds in all.
    EVIDENCE_ENRICH = setting(flag, True)
    EVIDENCE_ENRICH_TIMEOUT = setting(float, 20.0, minimum=0)

    # Logging
    LOG_LEVEL = setting(str, "INFO")

    # Days of request metrics and model events kept for the /admin dashboard,
    # in the ArcadeDB database. Only metadata (inputs and outputs are traces,
    # above). 0 records none.
    MONITORING_RETENTION_DAYS = setting(float, 7.0, minimum=0)

    @classmethod
    def needs_setup(cls) -> bool:
        """True until the setup wizard has run (or been skipped) or a key is configured."""
        return cls.SETUP_WIZARD and not cls.API_KEY and not cls.ADMIN_API_KEY

    @classmethod
    def apply(cls, values: Dict[str, str]) -> None:
        """Make settings written to the settings file take effect in this process: each one named is read again.

        What is read per request (keys, allowed models, limits) changes at
        once; what was used at startup, such as CORS_ORIGINS, on restart.
        """
        os.environ.update(values)
        _FROM_FILE.update(values)
        for name in values:
            if name in SETTINGS:
                setattr(cls, name, SETTINGS[name].read())

    @classmethod
    def described(cls) -> Dict[str, Any]:
        """Every setting's effective value, for /admin: secrets only as set or not, paths as text."""
        out: Dict[str, Any] = {}
        for name, spec in SETTINGS.items():
            value = getattr(cls, name)
            if spec.secret:
                value = "set" if value else "not set"
            elif isinstance(value, Path):
                value = str(value)
            out[name] = value
        return out

    @staticmethod
    def secret_names() -> List[str]:
        """The settings that are keys and secrets: hidden on /admin, kept out of child processes (procs.hide)."""
        return [name for name, spec in SETTINGS.items() if spec.secret]

    @classmethod
    def fatal(cls) -> List[str]:
        """Why the service cannot start as configured; main.py prints these and exits."""
        found = []
        if cls.WORKERS > 1 and cls.ARCADEDB_PATH:
            found.append("WORKERS > 1 with ARCADEDB_PATH set: only one process can open the embedded database, so "
                         "every worker but one would fail to start. Run one worker, or set ARCADEDB_PATH empty "
                         "(no /db, collections, traces, jobs or request history).")
        return found

    @staticmethod
    def set_by_environment(name: str) -> bool:
        """Whether a real environment variable sets ``name``; it then wins over the file on every start."""
        return bool(os.environ.get(name)) and name not in _FROM_FILE

    @classmethod
    def warnings(cls) -> List[str]:
        """Settings that are risky outside development, logged at startup and shown on /admin."""
        found = []
        if not cls.API_KEY:
            found.append("API_KEY is not set: the API accepts unauthenticated requests")
        elif not cls.ADMIN_API_KEY:
            found.append("ADMIN_API_KEY is not set: anyone with API_KEY can load, unload and clear models")
        if not cls.ALLOWED_MODELS:
            found.append("ALLOWED_MODELS is empty: clients can make the server download any Hub model")
        if "*" in cls.CORS_ORIGINS:
            found.append("CORS_ORIGINS is *: any website can call the API from a browser")
        if cls.DEBUG:
            found.append("DEBUG is on: error responses include exception text")
        if cls.MODEL_TURN_S and cls.MODEL_TURN_S >= cls.MODEL_WAIT_TIMEOUT:
            found.append("MODEL_TURN_S is not below MODEL_WAIT_TIMEOUT: a load gives up (503) before its turn comes")
        if cls.WORKERS > 1:
            # Possible only without the database (see fatal()).
            found.append("WORKERS > 1: each worker has its own cache (and starts its own model servers), "
                         "so /admin shows one worker's loaded models")
        return found
