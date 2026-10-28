# Core AI & Data toolkit

![status: alpha](https://img.shields.io/badge/status-alpha-orange)
![version: 0.2alpha22](https://img.shields.io/badge/version-0.2alpha22-blue)

A self-hosted service for small, efficient AI models (text encoders,
classifiers, question answering, speech, small vision and chat models) and the
data work around them. One process serves the models through a REST API and an
OpenAI-compatible one, and adds tables and graphs, vector search, background
jobs, evals, lookups in outside services, and an admin dashboard to run it all.

It is built for models that fit beside each other on one machine and for quick
scripts, not for large LLMs. For those, run a dedicated server (Ollama,
LM Studio, vLLM) and let the toolkit forward to it.

> **Status: alpha (0.2alpha22).** This is under active development. APIs,
> settings and stored data change without notice between versions.

## What it does

**Serve models**
- Loads any Hugging Face model on demand, in a cache budgeted by memory. Busy
  models are never evicted, and urgent requests go first when models compete for
  room.
- Single requests that arrive together share one forward pass (micro-batching),
  about nine times cheaper per text for a small classifier.
- OpenAI-compatible `/v1`: chat with images, audio, tools and JSON replies;
  completions with log probabilities (for lm-evaluation-harness); embeddings,
  reranking and transcription.
- Model servers: forwards a model to Ollama, LM Studio or a remote GPU box, or
  starts MLX, vLLM or llama.cpp itself, with fallback backends tried in order.
- A playground to chat with models side by side or try the other tasks.

**Store data**
- `/db`: namespaced tables of JSON rows, with schemas, bulk writes and
  transactions. Relationships (edges) link rows across tables, read by traversal or Cypher.
- Collections: documents embedded with a model, searched by meaning within SQL
  filters.
- Everything lives in one embedded ArcadeDB database, browsable from the
  dashboard, with a graph view.

**Run work in the background**
- Jobs: long-running scripts in their own process and Python environment, on a
  schedule if you like. AutoGluon training is included, and the trained
  predictors are served like models.
- Evals: datasets from traces, files or saved chats; runs that batch their
  examples and resume after a restart; scoring, with an optional LLM judge.
- One queue for jobs, eval runs, judging and downloads, with priorities;
  webhooks signed to the Standard Webhooks spec.

**Look things up**
- Enrichers: look an IP, domain, URL, file hash, email, CVE or company up in
  outside services. Each enricher is a small editable JSON definition of one HTTP
  call; 29 keyless or free-tier sources are built in.
- Evidence capture: web pages captured by a browser extension become signed,
  hash-chained, time-stamped records that can be verified later. The extension
  is not part of this repository.

**Operate it**
- Admin dashboard (`/admin`), Prometheus metrics (`/metrics`), request traces,
  and a setup wizard that writes the keys on first start.

## Requirements

- Python 3.11+ with [uv](https://docs.astral.sh/uv/) (or pip), and Node 22.12+
  to build the browser pages. Or just Docker or Podman.
- macOS (Apple Silicon GPUs through MPS) or Linux (CPU, or NVIDIA GPUs through
  CUDA). A container on macOS runs models on the CPU only.

## Quick start

**Local** (fastest on Apple Silicon):

```bash
make install    # Python dependencies into .venv, and the browser pages built
make run        # start on http://localhost:8000
```

**Container:**

```bash
make up                            # docker compose up --build -d
make up COMPOSE="podman compose"   # the same with Podman
make logs                          # follow the log
make down                          # stop; weights and settings stay in named volumes
```

On an NVIDIA host, uncomment the `deploy:` block in `docker-compose.yml` first.
More setup options are in the [guide](docs/GUIDE.md#setup-details).

On first start the log shows a link to the setup wizard
(`http://localhost:8000/admin/setup#code=...`). It generates the API keys,
picks the models clients may use, and writes them to `.env`. Then open:

- `http://localhost:8000/admin`: the dashboard
- `http://localhost:8000/playground`: chat with models and try them out
- `http://localhost:8000/docs`: the interactive API reference

`make` alone lists every command. Without make or uv:
`pip install -r requirements.txt`, `(cd frontend && npm ci && npm run build)`,
`python main.py`.

## First requests

Classify a sentence (the model downloads on first use):

```bash
curl -s http://localhost:8000/inference \
  -H "Authorization: Bearer $API_KEY" -H "Content-Type: application/json" \
  -d '{"model_id": "AnkitAI/Sensible-ModernBERT-Sentiment-Analysis",
       "task": "text-classification", "input": {"text": "I love this movie!"}}'
```

Chat through any OpenAI client:

```python
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8000/v1", api_key="<API_KEY>")
reply = client.chat.completions.create(
    model="Qwen/Qwen2.5-0.5B-Instruct",
    messages=[{"role": "user", "content": "Say hello in French."}],
)
print(reply.choices[0].message.content)
```

## Security

The service downloads and runs whatever model a request names, so do not expose
it without these settings (the setup wizard sets the first three):

- `API_KEY`: every endpoint except `/` and `/health` then needs
  `Authorization: Bearer <key>` or `X-API-Key: <key>`.
- `ADMIN_API_KEY`: the key for administration (the dashboard, loading and
  unloading models). Without it, `API_KEY` guards those too.
- `ALLOWED_MODELS`: the model IDs clients may use; others get `403`. Empty
  allows any Hugging Face model.
- `TRUST_REMOTE_CODE_MODELS`: the only repos allowed to run their own Python
  code. `TRUST_REMOTE_CODE=false` turns it off entirely.

Until the setup wizard has run, the API has no key: anyone who can reach the
port can use it. So `make run` listens on this machine only (`HOST=127.0.0.1`);
the container listens on every interface, so finish the setup before you open
its port to others.

Keys live in `.env`, which git ignores; `env.example` is the template.

## Configuration

Settings are environment variables, read from `.env` too. `env.example` lists
them with their defaults, and the [guide](docs/GUIDE.md#configuration)
explains them. The ones you are most likely to change: `DEFAULT_DEVICE`
(`auto`, `cpu`, `cuda`, `mps`), `MAX_MODELS` and `MAX_MEMORY_GB` (how many
models stay loaded, and in how much memory), and `PRELOAD_MODELS`.

## Documentation

- [docs/GUIDE.md](docs/GUIDE.md): every endpoint, every setting, and a section
  per feature (chat with images, tools, tables and graphs, enrichers, evals,
  model servers, model memory, troubleshooting).
- [STRUCTURE.md](STRUCTURE.md): how the code is organised and how a request is
  served.
- [models/README.md](models/README.md): adding a model, one decorated class per
  file.
- [jobs/README.md](jobs/README.md): writing a job and its environment.

## Development

```bash
make dev        # run with auto-reload
make test       # Python and frontend tests (no model downloads, no torch needed)
make lint       # ruff and the frontend's type check
cd frontend && npm run dev   # the pages with hot reload, calling the API on :8000
```

The pages are React and TypeScript, built with Vite. Their API types are
generated from the service's OpenAPI schema (`npm run gen:api`), and the tests
fail when that schema is out of date.

## License

MIT; see [LICENSE](LICENSE).
