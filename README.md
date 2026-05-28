# dhl2mh

Pipeline: Plenty orders → filter → DHL DeliverIT XML upload → label tracking number back to Plenty.
Python rewrite of the original C# project. Runs as a cronjob.

## Setup

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env  # then fill in real values
```

## Run

```bash
dhl2mh run            # full workflow once
```

## Test

```bash
pytest
```
