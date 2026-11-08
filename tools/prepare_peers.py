"""Descarga versiones fijas de los equipos en .peers, sin editar su código."""
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parent.parent
SOURCES = {
    "search": ("Mochytk/INF326-Arquitectura-de-Software_Tareas", "54940ec524ffc6f7ed3a3d7f6cf0801dcf43aa06",
               ["Tarea_Unidad_5_Despliegue"]),
    "media": ("667r/microservicios", "19f0ef79ecefbb6392b85be2571928e9dcedc0ea",
              ["03_Comunicación/demo/media"]),
    "polls": ("TomasCano-Usm/INF326-Microservicio-Polls", "76df544c42a6e1cc42fab490e1b200523faf0f10", []),
}


def main():
    for name, (repo, revision, folders) in SOURCES.items():
        target = ROOT / ".peers" / name
        if not (target / ".git").exists():
            subprocess.run(["git", "clone", "--filter=blob:none", "--no-checkout", "--depth", "1",
                            f"https://github.com/{repo}.git", str(target)], check=True)
        current = subprocess.run(["git", "-C", str(target), "rev-parse", "HEAD"],
                                 capture_output=True, text=True)
        required = target / folders[0] if folders else target / "Dockerfile"
        if current.returncode == 0 and current.stdout.strip() == revision and required.exists():
            print(f"{name}: listo", flush=True)
            continue
        subprocess.run(["git", "-C", str(target), "fetch", "--depth", "1", "origin", revision], check=True)
        if folders:
            subprocess.run(["git", "-C", str(target), "sparse-checkout", "set", "--cone", *folders], check=True)
        subprocess.run(["git", "-C", str(target), "checkout", "--detach", revision], check=True)
        print(f"{name}: listo", flush=True)


if __name__ == "__main__":
    main()
