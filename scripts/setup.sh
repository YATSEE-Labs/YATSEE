#!/usr/bin/env bash
# yatsee_setup.sh
#
# Bootstrap helper for local YATSEE development.
#
# This script is intentionally a convenience wrapper around:
#   - Python version checks
#   - virtualenv creation
#   - editable package install from pyproject.toml
#   - core system tool checks
#   - optional spaCy model install
#
# It is not the source of truth for Python dependencies. pyproject.toml is.

set -euo pipefail

echo "Starting YATSEE bootstrap..."

PYTHON_REQUIRED="3.11"
VENV_DIR=".venv"
DEFAULT_INSTALL_TARGET="full"
DEFAULT_SPACY_MODEL="en_core_web_md"

have_cmd() {
    command -v "$1" >/dev/null 2>&1
}

print_header() {
    printf '\n== %s ==\n' "$1"
}

python_version_ok() {
    local found_version
    found_version="$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"

    if [[ "$(printf '%s\n' "$PYTHON_REQUIRED" "$found_version" | sort -V | head -n1)" != "$PYTHON_REQUIRED" ]]; then
        echo "ERROR: Python ${PYTHON_REQUIRED}+ is required. Found ${found_version}."
        return 1
    fi

    echo "Python ${found_version} OK"
}

check_python_and_venv() {
    print_header "Python checks"

    if ! have_cmd python3; then
        echo "ERROR: python3 is not installed or not on PATH."
        exit 1
    fi

    python_version_ok

    if ! python3 -m venv --help >/dev/null 2>&1; then
        echo "ERROR: Python venv module is not available."
        exit 1
    fi

    echo "venv module available"
}

check_repo_files() {
    print_header "Repository checks"

    if [[ ! -f "pyproject.toml" ]]; then
        echo "ERROR: pyproject.toml not found. Run this from the YATSEE repository root."
        exit 1
    fi

    echo "Found pyproject.toml"
}

check_system_tools() {
    print_header "System tool checks"

    if have_cmd ffmpeg; then
        echo "ffmpeg found: $(command -v ffmpeg)"
    else
        echo "WARNING: ffmpeg not found."
        echo "  Needed for audio formatting/transcoding workflows."
        echo "  Linux: install via your package manager"
        echo "  macOS: brew install ffmpeg"
        echo "  Continue only if you do not plan to run audio formatting on this host."
    fi
}

create_and_activate_venv() {
    print_header "Virtual environment"

    if [[ ! -d "$VENV_DIR" ]]; then
        python3 -m venv "$VENV_DIR"
        echo "Created virtual environment at $VENV_DIR"
    else
        echo "Using existing virtual environment at $VENV_DIR"
    fi

    # shellcheck disable=SC1091
    source "$VENV_DIR/bin/activate"
    echo "Activated virtual environment"
    echo "Remember to run: source $VENV_DIR/bin/activate"
}

install_package() {
    print_header "Package install"

    local install_choice install_target
    local -a pip_args

    echo "Install targets:"
    echo "  1) base       -> pip install -e ."
    echo "  2) pipeline   -> pip install -e .[pipeline]"
    echo "  3) index      -> pip install -e .[index]"
    echo "  4) search     -> pip install -e .[search]"
    echo "  5) research   -> pip install -e .[research]"
    echo "  6) ui         -> pip install -e .[ui]"
    echo "  7) llamacpp   -> pip install -e .[llamacpp]"
    echo "  8) full       -> pip install -e .[full]"
    echo "  9) skip install"

    read -r -p "Choose install target [8]: " install_choice
    install_choice="${install_choice:-8}"

    case "$install_choice" in
        1) install_target="base" ;;
        2) install_target="pipeline" ;;
        3) install_target="index" ;;
        4) install_target="search" ;;
        5) install_target="research" ;;
        6) install_target="ui" ;;
        7) install_target="llamacpp" ;;
        8) install_target="$DEFAULT_INSTALL_TARGET" ;;
        9)
            echo "Skipping package install"
            return 0
            ;;
        *)
            echo "Unknown choice: $install_choice"
            exit 1
            ;;
    esac

    python -m pip install --upgrade pip

    if [[ "$install_target" == "base" ]]; then
        pip_args=(-e .)
    else
        pip_args=(-e ".[${install_target}]")
    fi

    echo "Installing with: pip install ${pip_args[*]}"
    python -m pip install "${pip_args[@]}"
    echo "Package install complete"
}

check_compute_device() {
    print_header "Compute device"

    if ! python -c "import torch" >/dev/null 2>&1; then
        echo "torch not installed in this environment; skipping CUDA/MPS detection"
        return 0
    fi

    python <<'PY'
import torch

device = "CPU"
if torch.cuda.is_available():
    device = "CUDA GPU"
elif getattr(torch.backends, "mps", None) and torch.backends.mps.is_available():
    device = "Apple MPS"

print(f"Detected processing device: {device}")
PY
}

maybe_install_spacy_model() {
    print_header "spaCy model"

    if ! python -c "import spacy" >/dev/null 2>&1; then
        echo "spaCy not installed in this environment; skipping model install"
        return 0
    fi

    read -r -p "Check/install spaCy model ${DEFAULT_SPACY_MODEL}? [Y/n]: " spacy_choice
    spacy_choice="${spacy_choice:-Y}"

    if [[ ! "$spacy_choice" =~ ^[Yy]$ ]]; then
        echo "Skipping spaCy model check"
        return 0
    fi

    if python -c "import importlib.util; raise SystemExit(0 if importlib.util.find_spec('${DEFAULT_SPACY_MODEL}') else 1)"; then
        echo "spaCy model ${DEFAULT_SPACY_MODEL} already available"
    else
        echo "Installing spaCy model ${DEFAULT_SPACY_MODEL}..."
        python -m spacy download "${DEFAULT_SPACY_MODEL}"
        echo "spaCy model ${DEFAULT_SPACY_MODEL} installed"
    fi
}

show_next_steps() {
    print_header "Next steps"

    cat <<'EOF'
Common commands:

  yatsee --help
  yatsee config --help
  yatsee config entity list
  yatsee config validate
  yatsee audio format --help
  yatsee audio transcribe --help
  yatsee transcript normalize --help
  yatsee intel run --help

If you are using editable install mode and the console script is not available yet, use:

  python -m yatsee.cli.main --help

Notes:
  - pyproject.toml is the source of truth for Python dependencies
  - ffmpeg is needed for audio formatting/transcoding
  - raw media should already exist in data/<entity>/downloads or be passed with --input-dir
  - acquisition/import tooling is outside the core YATSEE CLI
  - transcript/index workflows may require spaCy models and additional extras
  - intel workflows use provider settings such as llm_provider and llm_provider_url in yatsee.toml
EOF
}

show_summary() {
    print_header "Summary"

    echo "Bootstrap complete"
    echo "  Python required : ${PYTHON_REQUIRED}+"
    echo "  Virtualenv      : ${VENV_DIR}"
    echo "  pyproject.toml  : present"
    echo "  ffmpeg          : $(command -v ffmpeg || echo 'missing')"
}

main() {
    check_python_and_venv
    check_repo_files
    check_system_tools
    create_and_activate_venv
    install_package
    check_compute_device
    maybe_install_spacy_model
    show_summary
    show_next_steps
}

main "$@"