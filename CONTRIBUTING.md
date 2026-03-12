# Contributing

## Development Setup

```bash
cd /path/to/dgx_top
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

## Before Opening a PR

Run:

```bash
.venv/bin/python -m py_compile dgx_top.py dgxtop/*.py
.venv/bin/python -m unittest discover -s tests
```

## Scope

- Keep the dashboard Linux-first
- Preserve the compact terminal layout
- Prefer explicit metrics over decorative UI changes
- Keep GPU handling best-effort and robust when NVML fields are missing
