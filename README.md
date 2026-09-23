# OpenMTGData

OpenMTGData is early infrastructure for building reproducible, provenance-preserving, model-independent Magic: The Gathering datasets. The first intended source is the intentionally published 17Lands Public Datasets. The package currently provides only a development scaffold; it does not yet inspect or support any source schema, build a dataset, or publish a public dataset.

Large raw archives and derived datasets do not belong in Git. Raw input roots may live outside the repository and will be supplied explicitly to future tools. The repository-relative `data/raw/17lands/` location is only a possible convenience default. OpenMTGData is independent of any particular model, including Laya and MageZero. An observed human action, if represented by a future view, describes behavior and is not an optimal-action claim.

The architecture authority is [SPEC.md](SPEC.md); the staged implementation plan is [PLAN.md](PLAN.md).

## Development

Use Python 3.11 or newer. From a checkout, install the package and development tools:

```bash
python -m pip install -e ".[dev]"
```

Run the checks:

```bash
python -m pytest
ruff check .
ruff format --check .
mypy src/openmtgdata
```

The scaffold CLI supports help and version output only:

```bash
openmtgdata --help
openmtgdata --version
python -m openmtgdata --help
python -m openmtgdata --version
```

No dataset files are needed to install, import, or run these commands. The license for OpenMTGData source code has not yet been decided; it is separate from the licenses applicable to future input datasets.
