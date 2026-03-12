# Development

## Setup

```bash
cd /path/to/dgx_top
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

## Common Commands

```bash
.venv/bin/python -m py_compile dgx_top.py dgxtop/*.py
.venv/bin/python -m unittest discover -s tests
.venv/bin/python dgx_top.py
```

## Project Layout

- `dgx_top.py`: source launcher
- `dgxtop/`: package code
- `tests/`: unit tests for pure rendering / formatting logic
- `docs/`: project documentation

## Notes

- GPU stats depend on local NVIDIA driver support.
- Docker actions require access to the Docker socket.
- The UI is designed for Linux terminals and has not been adapted for Windows.
