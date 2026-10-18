with open('src/trading_agent/core/config.py', 'r') as f:
    content = f.read()

# Add get_config as alias for get_settings
old = 'def get_settings() -> Settings:'
new = '''def get_config() -> Settings:
    """Alias for get_settings() for backward compatibility."""
    return get_settings()


def get_settings() -> Settings:'''

new_content = content.replace(old, new)

with open('src/trading_agent/core/config.py', 'w') as f:
    f.write(new_content)
print('Done')