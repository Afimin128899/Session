"""Normalize connection settings without exposing credential values."""
class ProxyConfigError(ValueError):
    pass


def normalize_proxy(value):
    if value is None or value == {}:
        return None
    if not isinstance(value, dict):
        raise ProxyConfigError('Прокси должен быть объектом JSON с типом, адресом и портом.')
    kind = next((value[key] for key in ('proxy_type', 'type', 'protocol')
                 if key in value and value[key] is not None), 'socks5')
    aliases = {1:'socks4', 2:'socks5', 3:'http', '1':'socks4', '2':'socks5', '3':'http',
               'socks4':'socks4', 'socks5':'socks5', 'http':'http', 'socks5h':'socks5'}
    key = kind.strip().lower() if isinstance(kind, str) else kind
    if isinstance(key, bool) or not isinstance(key, (str, int)) or key not in aliases:
        raise ProxyConfigError('Некорректный тип прокси. Поддерживаются socks4, socks5, socks5h и http.')
    address = value.get('addr') or value.get('host') or value.get('ip')
    if not isinstance(address, str) or not address.strip():
        raise ProxyConfigError('В параметрах прокси отсутствует адрес addr/host/ip.')
    raw_port = value.get('port', 1080)
    if isinstance(raw_port, bool) or not isinstance(raw_port, (int, str)):
        raise ProxyConfigError('Порт прокси должен быть целым числом 1–65535.')
    try:
        port = int(raw_port)
    except (ValueError, TypeError):
        raise ProxyConfigError('Порт прокси должен быть целым числом 1–65535.') from None
    if not 1 <= port <= 65535:
        raise ProxyConfigError('Порт прокси должен быть целым числом 1–65535.')
    rdns = value.get('rdns', True)
    if not isinstance(rdns, bool):
        raise ProxyConfigError('Параметр rdns должен быть true или false.')
    username = value.get('username') or value.get('user')
    password = value.get('password') or value.get('pass')
    if any(item is not None and not isinstance(item, str) for item in (username, password)):
        raise ProxyConfigError('Логин и пароль прокси должны быть строками.')
    return {'proxy_type': aliases[key], 'addr': address.strip(), 'port': port,
            'username': username, 'password': password,
            'rdns': True if key == 'socks5h' else rdns}
