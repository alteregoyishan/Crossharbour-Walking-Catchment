"""Small, dependency-free reader for secrets stored in the project .env file."""
import os

from .paths import ROOT


def env_value(name, default=None):
    """Return an environment variable or its exact value from .env.

    Values are never printed or copied into output metadata.
    """
    value = os.getenv(name)
    if value:
        return value
    path = ROOT / '.env'
    if not path.exists():
        return default
    for raw in path.read_text(encoding='utf-8').splitlines():
        line = raw.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        if key.strip() == name:
            value = value.strip().strip('"').strip("'")
            return value or default
    return default


def os_project_key():
    """Return one OS Data Hub project key for every enabled OS API.

    Service-specific variables remain optional overrides for older local setups.
    """
    return (env_value('OS_API_KEY') or env_value('OS_PLACES_API_KEY') or
            env_value('OS_NGD_API_KEY') or env_value('OS_MAPS_API_KEY'))
