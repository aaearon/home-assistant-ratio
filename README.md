# Ratio EV Charging — Home Assistant Integration

Home Assistant integration for [Ratio](https://ratio.energy/) EV chargers,
backed by the [`aioratio`](https://github.com/aaearon/aioratio) async
client library.

## Status

Early scaffold. Entities will be refined against live captures.

## Install

### HACS (custom repository)

1. HACS → Integrations → three-dot menu → Custom repositories.
2. Add `https://github.com/aaearon/home-assistant-ratio` as type
   "Integration".
3. Install "Ratio EV Charging".
4. Restart Home Assistant.

### Manual

Copy `custom_components/ratio` into your Home Assistant `config/custom_components/`
directory and restart.

## Configure

Settings → Devices & Services → Add Integration → "Ratio EV Charging".
Enter the email and password you use with the Ratio mobile app.

## Tests

The repository ships pytest tests targeting `pytest-homeassistant-custom-component`.
To run them locally:

```
pip install pytest-homeassistant-custom-component
pytest
```

## License

MIT.
