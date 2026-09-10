from __future__ import annotations

import paramiko

from deploy_enterprise_update import load_connection, run


def main() -> None:
    host, port, username, password = load_connection()
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(host, port=port, username=username, password=password, timeout=20)
    output = run(client, "nginx -T 2>&1 | grep '^# configuration file'")
    client.close()
    print(output)


if __name__ == "__main__":
    main()
