# Architecture diagrams

Each diagram is a Mermaid source file (`*.mmd`) plus the SVG rendered from it. The
source is what you edit; the SVG is a build artefact that happens to be committed,
because GitHub will not render a Mermaid file referenced as an image and a README
that renders nothing is worse than a stale picture.

| Source | Rendered | Shown in |
|---|---|---|
| `pipeline.mmd` | `pipeline.svg` | ARCHITECTURE.md § Multi-agent orchestration |
| `registry.mmd` | `registry.svg` | ARCHITECTURE.md § The domain registry |
| `star_schema.mmd` | `star_schema.svg` | ARCHITECTURE.md § The warehouse |
| `governance.mmd` | `governance.svg` | ARCHITECTURE.md § Governance |

Regenerate after editing a `.mmd` (needs Node; nothing here depends on it at runtime):

    npm install -g @mermaid-js/mermaid-cli
    mmdc -i pipeline.mmd -o pipeline.svg -c mermaid.config.json -b white -w 1600

`-b white` is deliberate. GitHub serves the same SVG in both themes and does not
recolour it, so a transparent background renders dark text on dark grey for anyone
reading in dark mode.

These replaced two PNGs (`multi_agent_orchestration.png`,
`distributed_lakehouse_architecture.png`) that were drawn by hand, described the
pre-Phase-2 Streamlit-era design, and could not be diffed. If a diagram here drifts
from the code, the fix is one edit and one command.
