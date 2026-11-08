"""Verifica y graba la web contra el Compose aislado de demostración.

No usar contra datos existentes: el entorno completo debe eliminarse con down -v.
Gemini debe estar simulado mediante compose.demo-test.yaml.
"""
import argparse
from pathlib import Path
import shutil
import subprocess
import tempfile
from urllib.parse import urlparse
from uuid import uuid4

from playwright.sync_api import expect, sync_playwright


ROOT = Path(__file__).resolve().parent.parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:18080")
    parser.add_argument("--isolated", action="store_true", required=True, help="Confirma que es el entorno descartable de pruebas")
    parser.add_argument("--channel", default="msedge" if shutil.which("powershell") else None, help="Canal de navegador de Playwright")
    args = parser.parse_args()
    output = ROOT / "docs" / "evidencia"
    output.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix="hilo-recording-"))
    requests, errors = [], []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, channel=args.channel)
        context = browser.new_context(viewport={"width": 1440, "height": 900}, locale="es-CL",
                                      record_video_dir=str(temporary), record_video_size={"width": 1440, "height": 900})
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("request", lambda request: requests.append(request.url))
        page.goto(args.base_url, wait_until="networkidle")

        def chapter(text):
            page.evaluate("""text => {
              let label = document.getElementById('demo-caption');
              if (!label) {
                label = document.createElement('div'); label.id = 'demo-caption';
                label.style.cssText = 'position:fixed;bottom:10px;left:50%;transform:translateX(-50%);background:#172338ed;color:white;padding:10px 20px;border-radius:9px;font:12px system-ui;z-index:9999;pointer-events:none;box-shadow:0 4px 15px #0002';
                document.body.append(label);
              }
              label.textContent = text;
            }""", text)
            page.wait_for_timeout(1300)
            print(text, flush=True)

        chapter("01 · Chat integrado · APIs reales de los equipos · Gemini simulado para esta prueba")
        page.locator("#new-user").click()
        page.locator("#user-name").fill("Sofía")
        page.locator("#user-form button[type=submit]").click()
        expect(page.locator("#user-dialog")).not_to_be_visible()
        sofia = page.locator("#user-select").input_value()
        page.locator("#new-user").click()
        page.locator("#user-name").fill("Diego")
        page.locator("#user-form button[type=submit]").click()
        expect(page.locator("#user-dialog")).not_to_be_visible()
        diego = page.locator("#user-select").input_value()
        page.locator("#new-channel").click()
        channel_name = "proyecto-final-" + uuid4().hex[:5]
        page.locator("#channel-name").fill(channel_name)
        page.locator("#channel-form button[type=submit]").click()
        expect(page.locator("#channel-title")).to_have_text(channel_name)

        chapter("02 · Conversación · Crear un canal, elegir usuario y enviar mensajes")
        page.locator("#user-select").select_option(sofia)
        for message in ["Equipo, acordamos entregar el informe el viernes.",
                        "Me encargo de preparar los diagramas de arquitectura.",
                        "Reunión de revisión el jueves a las 10:00."]:
            page.locator("#message-input").fill(message)
            page.locator("#send-message").click()
            expect(page.locator("#message-input")).to_have_value("")
            page.wait_for_timeout(700)
        page.locator("#user-select").select_option(diego)
        page.locator("#message-input").fill("Me encargo de revisar las pruebas antes de la entrega.")
        page.locator("#send-message").click()
        expect(page.locator("#messages .message")).to_have_count(4)

        chapter("03 · Resúmenes · Solicitud asíncrona y resultado con decisiones, eventos y tareas")
        page.locator("#request-summary").click()
        expect(page.locator("#summary-status")).to_have_text("Listo", timeout=60000)
        expect(page.locator("#summary-content")).to_contain_text("Decisiones")
        expect(page.locator("#summary-content")).to_contain_text("Tareas")
        page.evaluate("document.getElementById('demo-caption').hidden = true; document.getElementById('toast').hidden = true")
        page.screenshot(path=str(output / "interfaz.png"), full_page=True)
        page.evaluate("document.getElementById('demo-caption').hidden = false")
        page.wait_for_timeout(2500)

        chapter("04 · Búsqueda del equipo · Texto, autor y orden sobre el canal")
        page.locator("#tab-search").click()
        page.locator("#search-query").fill("viernes")
        for _ in range(12):
            page.locator("#search-form button[type=submit]").click()
            page.wait_for_timeout(1500)
            if page.locator("#search-results .search-result").count():
                break
        expect(page.locator("#search-results")).to_contain_text("viernes")
        page.locator("#search-author").select_option(sofia)
        page.locator("#search-order").select_option("asc")
        page.locator("#search-form button[type=submit]").click()
        expect(page.locator("#search-count")).to_contain_text("1 resultado")
        page.wait_for_timeout(2000)

        chapter("05 · Medios del equipo · Subir, consultar, descargar y eliminar un archivo")
        page.locator("#tab-files").click()
        contents = "Acuerdos del equipo: entregar el informe el viernes.\n".encode()
        page.locator("#file-input").set_input_files({"name": "acuerdos-del-equipo.txt", "mimeType": "text/plain", "buffer": contents})
        expect(page.locator("#gallery")).to_contain_text("acuerdos-del-equipo.txt", timeout=15000)
        with page.expect_download() as transfer:
            page.get_by_role("button", name="Descargar ↓").first.click()
        assert Path(transfer.value.path()).read_bytes() == contents
        page.wait_for_timeout(1800)
        page.get_by_role("button", name="Eliminar", exact=True).first.click()
        expect(page.locator("#gallery")).not_to_contain_text("acuerdos-del-equipo.txt")

        chapter("Medios · Los enlaces del chat generan su vista previa en segundo plano")
        page.locator("#tab-messages").click()
        page.locator("#message-input").fill("Referencia del proyecto: https://www.python.org/")
        page.locator("#send-message").click()
        expect(page.locator("#message-input")).to_have_value("")
        page.locator("#tab-files").click()
        page.locator("#media-category").select_option("LINK")
        for _ in range(20):
            if page.locator("#gallery .file-card").count():
                break
            page.wait_for_timeout(2000)
            page.locator("#media-category").dispatch_event("change")
        expect(page.locator("#gallery .file-card")).to_have_count(1)
        expect(page.locator("#gallery")).to_contain_text("Python")
        page.wait_for_timeout(2000)

        chapter("06 · Encuestas del equipo · Crear, votar con otro usuario y cerrar")
        page.locator("#tab-polls").click()
        page.locator("#user-select").select_option(sofia)
        page.locator("#new-poll").click()
        page.locator("#poll-question").fill("¿Cuándo revisamos la entrega final?")
        page.locator("#poll-options").fill("Viernes a las 10:00\nViernes a las 15:00")
        page.locator("#poll-form button[type=submit]").click()
        expect(page.locator("#poll-dialog")).not_to_be_visible(timeout=15000)
        expect(page.locator("#poll-list")).to_contain_text("¿Cuándo revisamos la entrega final?")
        expect(page.locator("#poll-list")).to_contain_text("Abierta", timeout=20000)
        page.locator("#user-select").select_option(diego)
        page.locator("#poll-list input[type=radio]").first.check()
        page.get_by_role("button", name="Votar", exact=True).click()
        expect(page.locator("#poll-list")).to_contain_text("1 voto")
        page.wait_for_timeout(2000)
        page.locator("#user-select").select_option(sofia)
        page.get_by_role("button", name="Cerrar", exact=True).click()
        expect(page.locator("#poll-list")).to_contain_text("Cerrada")
        page.wait_for_timeout(2000)

        chapter("07 · El chat conserva los anuncios y los resultados de las encuestas")
        page.locator("#tab-messages").click()
        expect(page.locator("#messages")).to_contain_text("Encuesta finalizada", timeout=15000)
        page.wait_for_timeout(2500)

        chapter("08 · La interfaz también se adapta al teléfono")
        page.set_viewport_size({"width": 390, "height": 844})
        page.wait_for_timeout(1000)
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        page.locator("#mobile-menu").click()
        expect(page.locator("#sidebar")).to_have_class("sidebar open")
        page.locator("#channel-list [data-channel]").first.click()
        expect(page.locator("#sidebar")).not_to_have_class("sidebar open")
        page.wait_for_timeout(400)
        page.evaluate("document.getElementById('demo-caption').hidden = true")
        page.screenshot(path=str(output / "interfaz-movil.png"), full_page=True)
        page.evaluate("document.getElementById('demo-caption').hidden = false")
        page.wait_for_timeout(1500)
        page.set_viewport_size({"width": 1440, "height": 900})
        chapter("Verificación completada · Todas las lecturas y escrituras de la web pasan por el gateway")
        page.wait_for_timeout(1800)
        assert not errors, errors
        origin = urlparse(args.base_url).netloc
        assert all(urlparse(url).netloc == origin for url in requests if url.startswith(("http://", "https://")))
        video = page.video
        context.close()
        source = Path(video.path())
        browser.close()

    destination = output / "demo-interfaz.mp4"
    subprocess.run(["ffmpeg", "-y", "-i", str(source), "-c:v", "libx264", "-preset", "fast", "-crf", "27",
                    "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(destination)],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print(f"VIDEO={destination}")
    print(f"Peticiones del navegador al gateway: {len(requests)}. Errores JavaScript: {len(errors)}.")


if __name__ == "__main__":
    main()
