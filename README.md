# Potune

Potune is a lightweight Linux GUI for controlling:

- CPU governor
- Energy Performance Preference (EPP)
- ASUS platform power profiles

## Requirements

- Linux
- Python 3
- PyQt6
- x86_energy_perf_policy
- asusctl (for ASUS laptops)

## Run

```bash
python3 potune.py
```

Potune will request root permissions automatically when applying changes.
