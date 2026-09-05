# Aclara ACE Water Portal for Home Assistant

Pulls hourly water usage from an Aclara / SilverBlaze "ACE" customer portal
(`acewebsite.silverblaze.com`, used by a number of US water utilities) and
feeds it into Home Assistant's long-term statistics so it shows up in the
**Energy dashboard → Water consumption** with the hourly bars on the right day.

The portal publishes AMI reads roughly a day late, so a live "meter reading"
sensor would be permanently stale. Instead this integration imports historical
statistics (the same approach Home Assistant's Opower integration uses):

- `aclara_ace:water_<meter>_consumption` — hourly gallons, cumulative sum
- `aclara_ace:water_<meter>_cost` — hourly cost from a flat price you configure

It also creates one diagnostic sensor per meter, **Latest reading**, showing the
newest hour the portal has published, with the statistic ids as attributes.

## Install

### HACS (recommended)

1. HACS → Integrations → ⋮ → **Custom repositories**, add this repo as type *Integration*.
2. Install **Aclara ACE Water Portal**, restart Home Assistant.

### Manual

Copy `custom_components/aclara_ace` into `<config>/custom_components/` and restart.

## Configure

1. Settings → Devices & services → **Add integration** → *Aclara ACE Water Portal*.
2. Enter the portal email + password. The **client ID** is your utility's number
   in the portal's login URL (`acewebsite.silverblaze.com/B2C?clientId=NNN`);
   follow the "usage portal" link from your utility's website to find it.
3. On first refresh the integration backfills every hour the portal holds
   (since January 2026 for a meter installed then). Later refreshes run every
   6 hours and re-import the last 7 days so late or corrected reads are fixed up.
4. Optional: ⋮ → **Configure** on the integration to set a flat price per gallon
   for the cost statistic.

### Energy dashboard

Settings → Dashboards → Energy → **Water consumption** → *Add water source* and
pick `ACE water <meter> consumption`. For cost, choose *Use an entity tracking
the total costs* and pick `ACE water <meter> cost`.

## Command-line pull

`scripts/ace_pull.py` uses the same client to dump readings without Home Assistant:

```bash
uv venv && uv pip install aiohttp
echo 'ACE_USERNAME=you@example.com' >  .env
echo 'ACE_PASSWORD=secret'          >> .env
echo 'ACE_CLIENT_ID=NNN'            >> .env
.venv/bin/python scripts/ace_pull.py --info            # account + meters + data span
.venv/bin/python scripts/ace_pull.py --days 30          # summary JSON
.venv/bin/python scripts/ace_pull.py --start 2026-01-01 --csv > water.csv
```

## How it talks to the portal

1. `GET /B2C?clientId=NNN` → redirect to the Azure AD B2C sign-in page.
2. `POST .../SelfAsserted` with the email/password, then
   `GET .../api/CombinedSigninAndSignup/confirmed` returns an auto-submit form.
3. `POST /b2credirect` with that form → ASP.NET session cookies → dashboard.
4. `Menu/GetMenuWithAllSubTabs` + `api/Page/GetPageParameters` yield an opaque
   `Parameters` blob that every API call carries base64-encoded.
5. `api/DataDownload/ConsumptionData?meters=<id>|water|60min:usage|<id>&startDate=&endDate=`
   returns flat hourly rows in the utility's local time (end date exclusive).

Note: the portal's own usage *charts* (`api/ConsumptionV3/Data`) report exactly
double the hourly download for every day; the download endpoint matches the
daily register reads, so that is what this integration uses.

## Tests

```bash
uv pip install pytest-homeassistant-custom-component
.venv/bin/python -m pytest
```
