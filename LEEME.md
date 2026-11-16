# Dicema en las IAs — panel web

Panel privado en **https://ia.dicema.com.gt** que mide cómo aparece Dicema en ChatGPT, Claude, Gemini, Perplexity y Grok:

- **Corrida automática diaria** (6:15 a. m., hora de Guatemala) con todas las preguntas activas.
- **Pestañas:** Pulso, Respuestas (con la respuesta completa de cada IA), Fan-outs, Competencia, Brújula, Antes y después.
- **Sala de control:** correr preguntas o motores sueltos cuando quieras.
- **Preguntas:** agregar, editar y desactivar preguntas desde el panel.
- **Medición manual:** registrar o importar lo que se mide a mano (Copilot y apps reales). Siempre separado de lo que viene por API.

No usa librerías externas: solo Python. La base de datos es un archivo SQLite en el volumen `/data`.

---

## Desplegar en Easypanel

### 1. Subir el código a GitHub
Crea un repositorio **privado** (por ejemplo `dicema-ia`) y sube el contenido de esta carpeta. No subas ningún archivo `.env`.

### 2. Crear el servicio
En Easypanel: tu proyecto → **+ Service → App**, nombre `ia`.
- **Source:** GitHub → el repositorio → rama `main`.
- **Build:** Dockerfile (ya viene incluido).

### 3. Variables de entorno (pestaña Environment)
```
PANEL_PASSWORD=una-contraseña-larga-y-única
CLAVE_SECRETA=un-texto-largo-al-azar-de-40-caracteres-o-más
OPENAI_API_KEY=...
ANTHROPIC_API_KEY=...
GEMINI_API_KEY=...
PERPLEXITY_API_KEY=...
XAI_API_KEY=...
CORRIDA_DIARIA=06:15
```
- Si falta una clave de API, ese motor aparece como "falta clave" y no se corre.
- `CORRIDA_DIARIA=no` apaga la corrida automática.
- Para probar sin gastar en APIs: `SIMULAR=1` (usa respuestas de ejemplo; quítalo después).

### 4. Volumen (pestaña Mounts)
**Add Volume Mount** → nombre `datos`, mount path **`/data`**. Sin esto, los datos se borran en cada despliegue.

### 5. Dominio (pestaña Domains)
Agrega **`ia.dicema.com.gt`**, puerto **8000**, HTTPS activado. Easypanel genera el certificado solo.

### 6. DNS
Donde se administra el dominio `dicema.com.gt`, crea un registro:

| Tipo | Nombre | Valor |
|---|---|---|
| A | `ia` | IP pública del servidor de AWS (la Elastic IP) |

El grupo de seguridad de AWS debe permitir los puertos 80 y 443 (normalmente ya lo hace si Easypanel sirve otros sitios).

### 7. Desplegar
Presiona **Deploy**. Cuando el DNS propague, entra a https://ia.dicema.com.gt con la contraseña.

---

## Logo
Los logos oficiales ya están dentro de la app: `web/logo.png` (rojo, modo claro), `web/logo-blanco.png` (modo oscuro) y `web/icono.png` (ícono de la pestaña). El rojo de marca es **#E0222B**. Para cambiarlos, reemplaza esos archivos, súbelos al repositorio y vuelve a desplegar.

## Primer uso

1. **Importar la línea base manual.** En la hoja "Medición IA Dicema", pestaña Registro, descárgala como CSV. En el panel: **Medición manual → Importar**. Quedan las 62 respuestas de ChatGPT y Perplexity del 13 de noviembre.
2. **Prueba corta por API.** Sala de control → **Ninguna** → marca M01 y L01 → **Correr**. Son 10 consultas. Revisa en Respuestas que cada motor contestó sin error.
3. **Corrida completa.** Sala de control → **Todas las activas** → Correr, con la nota "línea base API". Después de eso, la corrida diaria se encarga.

## Costos
Cada corrida completa son 52 preguntas × 5 motores = 260 consultas con búsqueda web. Pon un **límite de gasto mensual** en la consola de cada proveedor. Si el costo diario es alto, cambia `CORRIDA_DIARIA` o desactiva preguntas.

## Si algo falla
- **No entra con la contraseña:** revisa `PANEL_PASSWORD` y vuelve a desplegar.
- **La app no arranca:** en Logs aparece "Falta la variable PANEL_PASSWORD".
- **Un motor da HTTP 401:** clave incorrecta. **HTTP 404 o "model not found":** el proveedor cambió el nombre del modelo; agrega la variable `MODELO_CHATGPT`, `MODELO_CLAUDE`, `MODELO_GEMINI` o `MODELO_GROK` con el nombre nuevo, sin tocar el código.
- **HTTP 429:** límite de uso; el panel reintenta solo.
- Los modelos y formatos de cada API se tomaron de su documentación el 13 de noviembre de 2026. Cada respuesta original queda guardada en la base, así que se puede volver a analizar si un proveedor cambia su formato.

## Archivos
| Archivo | Para qué |
|---|---|
| `app.py` | Servidor web, contraseña y rutas. |
| `corredor.py` | Corridas en segundo plano y corrida diaria. |
| `motores.py` | Llamadas a cada API. |
| `analisis.py` | Detección de mención, posición, citas y confusiones. |
| `panel_datos.py` | Cálculos de cada pestaña. |
| `db.py` | Base de datos. |
| `web/` | Pantallas del panel. |
| `config.json` | Modelos, ubicación, términos de la marca y competidores. |
| `prompts_iniciales.csv` | Las 52 preguntas con que arranca la base. |
| `pruebas/` | Respuestas de ejemplo para el modo simulación. |
