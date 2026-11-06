"""One bounded, secret-free reason for a server that could not start.

Startup errors stay unprinted (driver messages can carry hosts and credentials), but a bare
"startup unavailable" hid a missing image package for a whole rollout (#546). The reason is the
exception class plus the missing module or a short identifier code, never free text.
"""

import re

_CODE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}")


def reason(error):
    name = type(error).__name__
    if isinstance(error, ImportError) and error.name and re.fullmatch(r"[A-Za-z_][\w.]{0,127}", error.name):
        return f"{name}:{error.name}"
    code = getattr(error, "code", None) or (error.args[0] if error.args else None)
    return f"{name}:{code}" if isinstance(code, str) and _CODE.fullmatch(code) else name


if __name__ == "__main__":
    assert reason(ModuleNotFoundError("x", name="bravo_apply")) == "ModuleNotFoundError:bravo_apply"
    assert reason(ValueError("invalid_product_configuration")) == "ValueError:invalid_product_configuration"
    assert reason(RuntimeError("could not connect to host db.internal password=x")) == "RuntimeError"
    print("ok")
