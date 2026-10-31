"""Siembra datos ficticios en el core desplegado. Cada POST se hace una sola vez; se detiene ante cualquier error."""
import json
import sys

import httpx

BASE = "http://{}.servicios-base.svc.cluster.local:8000"
http = httpx.Client(timeout=10)


def post(service, path, body):
    r = http.post(BASE.format(service) + path, json=body)
    print(f"POST {service}{path} -> {r.status_code}")
    if r.status_code != 201:
        print(r.text[:500])
        sys.exit(f"Respuesta inesperada de {service}; me detengo sin reintentar.")
    return r.json()


users = {name: post("users", "/api/v1/users", {"display_name": f"resumenes-test-{name}"})
         for name in ("ana", "bruno", "carla")}
for name, u in users.items():
    print(f"  usuario {name}: {json.dumps(u)}")

channel = post("channels", "/api/v1/channels", {"name": "resumenes-test-planificacion"})
print(f"  canal: {json.dumps(channel)}")

conversation = [
    ("ana", "Hola equipo, armemos la planificación del lanzamiento de la versión 2.0 de la app ficticia."),
    ("bruno", "Propongo lanzar el viernes 13 de noviembre; el martes 10 no alcanzamos con las pruebas."),
    ("carla", "De acuerdo con el viernes 13. ¿Quién se encarga de las notas de la versión?"),
    ("ana", "Decidido entonces: el lanzamiento es el viernes 13 de noviembre a las 10:00."),
    ("bruno", "Yo redacto las notas de la versión, las tengo listas el miércoles 11."),
    ("carla", "Yo me encargo de las pruebas de regresión en el ambiente de staging hasta el jueves 12."),
    ("ana", "Perfecto. Yo coordino con soporte para que estén atentos el día del lanzamiento."),
    ("bruno", "Otra cosa: descartamos la integración con el sistema de pagos para esta versión, queda para la 2.1."),
    ("carla", "Bien, eso reduce riesgos. ¿Hacemos una reunión de seguimiento?"),
    ("ana", "Sí, reunión de seguimiento el jueves 12 a las 16:00 para dar el visto bueno final."),
    ("bruno", "Anotado. Si las pruebas fallan, posponemos al lunes 16."),
    ("carla", "De acuerdo con el plan de contingencia. Nos vemos el jueves."),
]
for author, content in conversation:
    post("messages", f"/api/v1/channels/{channel['id']}/messages",
         {"author_id": users[author]["id"], "content": content})

print("RESULT " + json.dumps({"channel_id": channel["id"], "user_ids": {k: v["id"] for k, v in users.items()}}))
