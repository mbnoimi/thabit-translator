import os
import sys
import configparser


def _possible_config_paths():
    """The ordered config lookup the CLI and the auto-workflow use.

    Packaged installs (pipx, or any caller that wants to own the config) point
    THABIT_CONFIG at their own file: next to the installed package the file
    would be root-owned or absent. Unset, behaviour is unchanged - a source
    checkout still finds core/thabit_translator.conf first.
    """
    paths = []
    env_conf = os.environ.get('THABIT_CONFIG')
    if env_conf:
        paths.append(env_conf)
    paths += [
        # ../.. from this file = the checkout's core/ (where
        # thabit_translator.conf sits). Under pipx this lands in
        # site-packages/ and simply does not exist, so the search
        # continues with the per-user paths below.
        os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'thabit_translator.conf'),
        os.path.expanduser('~/.config/thabit/thabit_translator.conf'),
        os.path.expanduser('~/.thabit_translator.conf'),
    ]
    return paths


def _create_default_config():
    """Copy the shipped template to ~/.config/thabit/ on first run.

    Returns the created path, or None when the template is unavailable or the
    user's config directory is not writable - load_config() then simply falls
    back to the built-in defaults, exactly as before.
    """
    package_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    template = os.path.join(package_dir, 'thabit_translator.conf.template')
    if not os.path.exists(template):
        return None
    target = os.path.expanduser('~/.config/thabit/thabit_translator.conf')
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(template, encoding='utf-8') as src:
            content = src.read()
        if os.path.exists(target):
            return None
        with open(target, 'w', encoding='utf-8') as dst:
            dst.write(content)
        print(f"[INFO] Created your settings file: {target}")
        print("[INFO] Edit it to add your provider API keys, languages and cache preferences.")
        return target
    except OSError as e:
        print(f"[WARN] Could not create config {target}: {e}")
        return None


def ensure_user_config():
    """Materialise the per-user config on first run so there is always a file
    to edit, even when the first command only prints help or opens the menu.

    Creates ~/.config/thabit/thabit_translator.conf from the shipped template
    only when no config exists anywhere yet. Callers that already decide their
    own config location are left untouched: an explicit -c/--config argument
    (what the Jellyfin plugin always passes) and a THABIT_CONFIG environment
    variable both suppress creation. Returns the created path, or None.
    """
    if any(arg in ("-c", "--config") or arg.startswith("--config=") for arg in sys.argv[1:]):
        return None
    if os.environ.get('THABIT_CONFIG'):
        return None
    if any(os.path.exists(path) for path in _possible_config_paths()):
        return None
    return _create_default_config()


def load_config(config_path=None):
    """Load configuration from .conf file with defaults."""
    default_config = {
        'opensubtitles': {
            'api_key': '',
            'username': '',
            'password': '',
            'default_language': 'ar',
            'user_agent': 'ThabitTranslator v1.0'
        },
        'subdl': {'api_key': ''},
        'subsource': {'api_key': ''},
        'settings': {
            'default_source_lang': 'en',
            'default_target_lang': 'ar',
            'verbose': False,
            'cache_dir': os.path.expanduser('~/.cache/thabit_translator'),
            'last_folder': None,
            'media_library_paths': ''
        },
        'providers': {'enabled': 'opensubtitles,subdl,subsource'}
    }
    
    if config_path is None:
        config_path = None
        for path in _possible_config_paths():
            if os.path.exists(path):
                config_path = path
                break
        if config_path is None:
            # First run on a machine without any config (typical pipx install):
            # materialise the shipped template so there is a documented file to
            # put provider API keys into. Skipped whenever a path was given
            # explicitly (-c), which is what the Jellyfin plugin always does.
            config_path = _create_default_config()

    if config_path and os.path.exists(config_path):
        try:
            config = configparser.ConfigParser()
            config.read(config_path, encoding='utf-8')
            for section in default_config:
                if config.has_section(section):
                    for key in default_config[section]:
                        if config.has_option(section, key):
                            if isinstance(default_config[section][key], bool):
                                default_config[section][key] = config.getboolean(section, key)
                            else:
                                val = config.get(section, key).strip().strip('"').strip("'")
                                if val.startswith('~'):
                                    val = os.path.expanduser(val)
                                default_config[section][key] = val
            if default_config['settings'].get('verbose'):
                print(f"[INFO] Loaded config from: {config_path}")
        except Exception as e:
            print(f"[WARN] Failed to parse config: {e}. Using defaults.")
    default_config['_config_path'] = config_path
    return default_config

def extract_imdb_id(text):
    import re
    match = re.search(r'tt(\d{7,8})', text)
    return f"tt{match.group(1)}" if match else None