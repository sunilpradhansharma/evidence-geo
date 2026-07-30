# GEO curated source — editing guide

These YAML files are the **human / Medical-Affairs source of truth** for the GEO
ground-truth corpus. Editing a file here and regenerating updates the JSON-LD the
Chairman falls back to (and the machine-readable `llms.txt`). No code change needed.

## Pipeline

```
source/*.yaml  ──(scripts.generate_geo_schema)──▶  schema/*.json  +  llms.txt
   (you edit)        curated wins; openFDA seeds        (generated — do not hand-edit)
                     label fields where a curated
                     value is absent
```

## Editing a brand

1. Edit `source/<brand>.yaml`. Curated values **override** anything seeded from openFDA.
   Leave a field absent to let the FDA label seed it.
2. Regenerate + re-seed from openFDA (run with `cwd = backend/`):
   ```
   python -m scripts.generate_geo_schema
   ```
   Add `--no-seed` to skip the network, `--brand <name>` for one brand, `--dry-run` to
   preview without writing.
3. In a **running** backend, apply it live without a restart: `POST /api/geo/refresh`.

## Placeholder clinical values (IMPORTANT)

The `efficacy` percentages and `competitors` lists shipped as **POC placeholders**.
Until Medical Affairs signs them off, each file has:

```yaml
clinical_values_verified: false
```

While this is `false`:
- the generated `dataSource` says *"clinical values pending Medical-Affairs verification"*,
- `provenance.clinicalValuesVerified` is `false`, and
- the app UI shows a **"Placeholder clinical values (pending MA verification)"** caveat.

To verify a brand: replace the `efficacy` + `competitors` values with the approved
figures, set `clinical_values_verified: true`, and regenerate. Gate the whole corpus with:

```
python -m scripts.generate_geo_schema --check   # exits non-zero if anything is still unverified
```

## Note on label-derived data

Indications/adverse-reaction/dosing **text** and the boxed warning under
`schema/*.json → labelReference` come straight from the official FDA label (openFDA /
DailyMed) and carry an SPL id + effective date in `provenance` — that part is already real
and is **not** gated by `clinical_values_verified`.
