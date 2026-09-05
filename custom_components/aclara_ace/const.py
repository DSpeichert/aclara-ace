"""Constants for the Aclara ACE integration."""

from datetime import timedelta

DOMAIN = "aclara_ace"

CONF_CLIENT_ID = "client_id"
CONF_PRICE_PER_UNIT = "price_per_unit"

DEFAULT_PRICE_PER_UNIT = 0.0

# The portal publishes hourly AMI reads roughly a day late; polling more often
# than a few times a day only re-downloads the same rows.
UPDATE_INTERVAL = timedelta(hours=6)

# On every poll re-import this many days before the newest stored statistic so
# late-arriving or corrected reads overwrite what was imported earlier.
LOOKBACK_DAYS = 7

# If the portal doesn't report how far back AMI data goes, backfill this much.
DEFAULT_BACKFILL_DAYS = 365
