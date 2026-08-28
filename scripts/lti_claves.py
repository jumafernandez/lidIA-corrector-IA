"""Genera el par de claves con el que LidIA firma ante el campus. Idempotente."""
import pathlib
import subprocess
import sys

DESTINO = pathlib.Path(__file__).resolve().parent.parent / "data" / "lti"


def main():
    DESTINO.mkdir(parents=True, exist_ok=True)
    priv, pub = DESTINO / "privada.pem", DESTINO / "publica.pem"
    if priv.exists() and pub.exists():
        print(f"Ya existen en {DESTINO}. No se tocan: regenerarlas obligaría a "
              "reconfigurar la herramienta en el campus.")
        return 0
    subprocess.run(["openssl", "genrsa", "-out", str(priv), "2048"], check=True,
                   stderr=subprocess.DEVNULL)
    subprocess.run(["openssl", "rsa", "-in", str(priv), "-pubout", "-out", str(pub)],
                   check=True, stderr=subprocess.DEVNULL)
    priv.chmod(0o600)
    print(f"Generadas:\n  {priv} (privada, permisos 600 — no se comparte)\n  {pub} (pública)")
    print("\nLa pública es la que se pega en el campus al registrar la herramienta.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
