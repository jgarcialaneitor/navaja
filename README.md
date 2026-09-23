<div align="center">

# 🔪 navaja

**Servidor MCP para investigación personal de jurisprudencia en [CENDOJ](https://www.poderjudicial.es/search/indexAN.jsp)**

[![Tests](https://img.shields.io/badge/tests-367%20passed-brightgreen)](#-desarrollo)
[![Python](https://img.shields.io/badge/python-3.12+-blue)](#-desarrollo)
[![MCP](https://img.shields.io/badge/MCP-7%20herramientas-8A2BE2)](#-qué-es-navaja)

*Pregunta por jurisprudencia, recibe candidatas con metadatos y los resúmenes automáticos del propio sitio, y lee el texto completo de las resoluciones que elijas.*

</div>

---

## 📑 Índice

| | |
| --- | --- |
| [🧭 Qué es navaja](#-qué-es-navaja) | [📄 Texto completo](#-cómo-funciona-la-descarga-de-texto-completo) |
| [🔍 Filtros de búsqueda](#-filtros-de-búsqueda) | [📦 Descargas por lotes](#-descargas-por-lotes) |
| [📚 Materia](#-materia) | [💾 Guardado de PDF](#-guardado-de-pdf) |
| [📍 Localización](#-localización) | [💻 Funciona sin pantalla](#-funciona-sin-pantalla) |
| [📊 Paginación y el techo de 200](#-paginación-y-el-techo-de-200-registros) | [🔧 Configuración](#-configuración) |
| [🚫 Cuando el sitio no responde](#-cuando-el-sitio-no-responde) | [🚀 Instalación](#-instalación) |
| [🔒 Reglas de seguridad](#-reglas-de-seguridad) | [🧪 Desarrollo](#-desarrollo) |
| [🌹 Agradecimientos](#-agradecimientos) | |

---

## 🧭 Qué es navaja

`navaja-mcp` expone **ocho herramientas** para un cliente MCP:

| Herramienta | Qué hace | ¿Captcha? |
| --- | --- | :---: |
| `buscar_sentencias` | Busca por texto libre y por los filtros avanzados del sitio | ❌ No |
| `listar_localizaciones` | Devuelve el vocabulario de localizaciones del propio sitio, listo para reutilizar | ❌ No |
| `ver_texto_completo` | Descarga el texto completo de una resolución, bloqueando hasta tenerlo | ⚠️ Puede |
| `iniciar_descargas` | Arranca un lote de descargas sin bloquear | ⚠️ Puede |
| `estado_descargas` | Consulta barata del estado del lote | ❌ No |
| `recoger_descarga` | Recoge el resultado de un trabajo terminado | ❌ No |
| `cancelar_lote` | Cancela los trabajos en cola de un lote trabado | ❌ No |
| `estado_servidor` | Instantánea de la configuración en ejecución | ❌ No |

La búsqueda, los metadatos y los resúmenes automáticos salen directamente de la página pública de resultados. **Solo el paso de texto completo** puede disparar el captcha `Control Descargas masivas` del sitio.

Cada resultado expone los metadatos propios de la página, incluidos `roj`, `ecli`, `sede`, `municipio`, `ponente` y `sala`. La `sala` (`Sala de lo Civil`, `Sala de lo Penal`, `Sala de lo Social`) solo aparece en resoluciones del Tribunal Supremo (`STS`/`ATS`); para el resto, `sala` vale `null`.

---

## 🔍 Filtros de búsqueda

`buscar_sentencias` acepta texto libre más los filtros que ofrece el formulario avanzado del sitio. Cada uno fue **verificado contra el endpoint real, midiendo**, no leyendo el front-end.

| Argumento | Campo del sitio | Valores aceptados |
| --- | --- | --- |
| `texto` | `TEXT` | texto libre; opcional si hay otro criterio |
| `fecha_desde` / `fecha_hasta` | `FECHARESOLUCIONDESDE` / `FECHARESOLUCIONHASTA` | `AAAA-MM-DD` o `DD/MM/AAAA` |
| `jurisdiccion` | `JURISDICCION` | `CIVIL`, `PENAL`, `CONTENCIOSO`, `SOCIAL`, `MILITAR` |
| `tipo_resolucion` | `TIPORESOLUCION` | `SENTENCIA`, `AUTO` |
| `roj` / `ecli` | `ROJ` / `ECLI` | identificador exacto |
| `num_resolucion` / `num_recurso` | `NUMERORESOLUCION` / `NUMERORECURSO` | tal y como figura en la resolución |
| `ponente` | `PONENTE` | nombre del magistrado |
| `voces` | `VOCES` | vocabulario de materias, p. ej. `TRÁFICO DE DROGAS` |
| `localizacion` | `VALUESCOMUNIDAD` | ver [abajo](#-localización) |

| `coleccion` | `databasematch` | `AN` (todas las jurisdicciones), `TS` (solo Tribunal Supremo) |
| `orden` | `sort` | `reciente`, `antiguo` |
| `campos_extra` | cualquier otro | campos crudos del formulario, aplicados al final |

> [!IMPORTANT]
> Los filtros se combinan con **AND**. La coincidencia de términos del sitio también es AND y **no tiene ranking por relevancia**, así que los resultados vuelven ordenados por fecha de resolución, y lo que recuperes en una consulta depende de cómo la redactes. El sitio **corta cualquier consulta en 200 registros**: por eso una búsqueda amplia devuelve el mismo techo con cualquier redacción.

### 📚 Materia

El sitio clasifica cada resolución, y la clasificación viaja dentro del resumen como `RESUMEN: <etiqueta>`. El campo `materia` la expone por separado. Vale `null` cuando el sitio no clasificó la resolución: tanto `DELITO SIN ESPECIFICAR` como `MATERIAS NO ESPECIFICADAS` significan eso.

> [!WARNING]
> **No existe filtro de materia en el servidor.** Esto está medido, no supuesto:
>
> - `MATERIAS` es un campo del formulario del propio sitio, pero **el endpoint lo ignora**. Tres valores distintos, incluido uno válido, devolvieron la línea base sin cambios.
> - Los operadores de texto libre no sirven de nada: `"tráfico de drogas"` entre comillas equivale a la consulta sin comillas, `+tráfico +drogas` es peor, y `AND` / `Y` no cambian nada.
> - `voces` **sí** se respeta, pero es un tesauro distinto. No incluye todas las resoluciones cuya etiqueta dice `TRÁFICO DE DROGAS`, así que se deja fuera resultados relevantes.

**Cómo responder entonces a «las últimas N sobre X en Y»:** pon el lugar en `localizacion`, una redacción del tema en `texto`, pide una página de 10 a 50, y **clasifica los resultados por `materia`**.

Una consulta por `"tráfico de drogas"` también devuelve resoluciones sobre alcoholemias o extranjería que simplemente mencionan la frase. La etiqueta es lo que las distingue.

### 📍 Localización

`localizacion` acepta una lista. Cada entrada es o bien la forma del propio sitio, con el nivel en el sufijo, o bien un nombre de lugar a secas, que significa comunidad autónoma:

```python
buscar_sentencias(texto="tráfico de drogas", localizacion=["MELILLA(C)"])
buscar_sentencias(localizacion=["Barcelona(P)", "Melilla(S)"])
buscar_sentencias(localizacion=["Melilla"])   # equivale a "MELILLA(C)"
```

| Sufijo | Nivel |
| :---: | --- |
| `(C)` | Comunidad autónoma |
| `(P)` | Provincia |
| `(S)` | Sede |

Las entradas se combinan entre sí con **OR**, y con el resto de filtros con **AND**. Los nombres son el vocabulario en mayúsculas del sitio, que este publica en:

```
POST /search/jurisprudencia.action
  action=getComunidades&field=COMUNIDAD|PROVINCIA|SEDE&publicinterface=true
```

y que devuelve pares `CLAVE&ETIQUETA` separados por barras verticales (`MELILLA&MELILLA`, `PAÍS VASCO&PAÍS VASCO`).

`listar_localizaciones` expone ese vocabulario como herramienta, ya formateado en tokens que puedes devolver tal cual:

```python
listar_localizaciones(nivel="COMUNIDAD")                        # "MELILLA(C)", ...
listar_localizaciones(nivel="PROVINCIA", comunidad="MELILLA")   # "MELILLA(P)"
listar_localizaciones(nivel="SEDE", comunidad="MELILLA", provincia="MELILLA")
```

Los tokens siempre llevan su sufijo de nivel, así que un token de provincia no puede confundirse con uno de comunidad. Si falta el padre, lanza error en vez de devolver una lista vacía.

> [!NOTE]
> El valor es **la etiqueta que muestra el sitio**, no un identificador interno: una búsqueda con Melilla seleccionada envía `MELILLA(C) | `. Los códigos que el front-end guarda en sus propias casillas (`ALL@ALL@MELILLA`) los ignora el servidor, así que un cliente que los mande recibe resultados **sin filtrar y sin ningún error**.

### 📊 Paginación y el techo de 200 registros

`records_por_pagina` es 10, 20, 30 o 50. `pagina` es un número de página de verdad: navaja lo convierte al desplazamiento del sitio, que cuenta **registros**, no páginas.

Una página cuya ventana pasaría del registro 200 se **rechaza** en lugar de pedirse, porque más allá del techo el sitio recorta el desplazamiento en silencio y devuelve otra vez el conjunto entero: 200 registros con pinta de página normal.

> [!CAUTION]
> Equivocarse aquí no es hipotético. **Una versión anterior enviaba el número de página como desplazamiento**, así que la página 2 repetía nueve de los diez resultados de la página 1.

`total_capped` avisa cuando `total` está pegado a ese techo, o sea que el número es un **tope** y no un recuento. Cuando vale `true`, une varias redacciones del tema o acota con un rango de fechas, en vez de paginar: pasado el techo no hay nada más que alcanzar.

### 🚫 Cuando el sitio no responde

Una búsqueda que no encuentra nada **es una respuesta**: devuelve una página de resultados vacía. Hay otros tres desenlaces que sí son errores, distinguidos por el cuerpo de la respuesta porque sus formas tienen el mismo tamaño que las legítimas:

| Situación | Excepción |
| --- | --- |
| El sitio rechaza la petición como inválida | `SearchRequestError` |
| El sitio sirve su desafío `Control de grandes paginaciones` | `SearchGatedError` |
| El sitio devuelve más registros de los pedidos | `SearchError` |

navaja **no resuelve ese desafío**, y **nunca** presenta una búsqueda rechazada como un resultado vacío.

### 🔧 No modelado

`ID_NORMA`, `SUBTIPORESOLUCION`, `INSTITUCION`, `SECCION`, `SECCIONAUTO`, `SECCIONSOLOPLENO`, `TIPOORGANOPUB` y los indicadores `TIPOINTERES_*` son accesibles vía `campos_extra`.

`ID_NORMA` necesita un espacio de identificadores que navaja no sabe direccionar: un id que no coincide vuelve como una página legítima de cero resultados, no como un error.

---

## 📄 Cómo funciona la descarga de texto completo

Cuando pides el texto completo de una resolución, navaja solicita el documento a CENDOJ. Si el sitio responde con su página de captcha, navaja muestra la imagen a través de un **formulario HTTP local de larga vida**, atado a una interfaz de red privada, y espera a que **una persona** escriba la respuesta.

La URL del formulario tiene tres estados:

| Estado | Qué sirve |
| --- | --- |
| Hay un desafío pendiente | El formulario del captcha |
| No hay desafío pendiente | Una página en espera que se autorrefresca cada 5 segundos |
| No responde nada | No hay ningún proceso navaja corriendo |

Una pestaña abierta recoge el siguiente desafío **sin que la persona recargue**.

El listener sigue atado durante toda la vida del proceso, no solo durante un desafío. Es una **concesión deliberada**: un listener local algo más duradero a cambio de una URL que siempre responde algo útil. Aun así solo se ata a la interfaz privada validada (`0.0.0.0`, `::` y hosts vacíos se rechazan), y el token es obligatorio en todas las rutas. Un token incorrecto devuelve 404 en los dos estados.

La herramienta `estado_servidor` informa de si el listener está atado (`captcha_listening`) y de la URL real y usable del formulario (`captcha_url`). Ese dato solo se afirma cuando puede respaldarlo: si hay un listener vivo en este mismo proceso, no se toca la red; si no lo hay, navaja consulta su propio endpoint `/{token}/whoami` (con timeout acotado) y acepta un único JSON con un nonce por proceso:

| Clave | Significado |
| --- | --- |
| `pid` | PID de este proceso |
| `nonce` | Nonce del listener local registrado, o `null` |
| `listener_conflict` | `true` cuando el puerto lo sostiene **otro** proceso navaja-captcha con otro nonce: la URL publicada apunta a ese otro proceso y abrirla mostrará su página en espera — los captchas que se manden ahí nunca llegarán a este proceso |
| `listener_pid` | El PID que reporta el listener en conflicto, o `null` |
| `captcha_listening` | Solo `true` si este proceso puede probar que el listener responde con su propio nonce |

> [!TIP]
> Si `listener_conflict` es `true`, suele haber dos navaja viejos corriendo a la vez (o un puerto ocupado por otro programa con `NAVAJA_CAPTCHA_PORT` sin cambiar): cerrá el proceso indicado en `listener_pid`, o cambiá de puerto con `NAVAJA_CAPTCHA_PORT`.

> [!TIP]
> La URL forma parte de la API **a propósito**: quien llama puede obtenerla de `estado_servidor` antes de lanzar ninguna descarga bloqueante, dársela a la persona, y esta la abre una vez y la deja abierta. El token de la URL controla el acceso al formulario HTTP; no pretende ocultarse de quien llama, porque cualquiera con acceso local ya puede leerlo del fichero de token persistido.

### Fallos estructurados

`ver_texto_completo` devuelve fallos con un `error_code`:

| `error_code` | Significado |
| --- | --- |
| `captcha_timeout` | La persona no respondió a tiempo |
| `captcha_busy` | Ya hay un desafío pendiente en el listener |
| `captcha_rejected` | El sitio rechazó la respuesta |
| `full_text_error` | Otro fallo de descarga |
| `invalid_url` | La URL no se puede interpretar |

`captcha_timeout` y `captcha_busy` incluyen `captcha_url` en la carga útil, para que quien llama pueda abrir el formulario sin leer stderr.

El valor por defecto de `espera_segundos` es `120`, deliberadamente muy por debajo del timeout de petición configurado en el cliente MCP, de modo que el servidor tenga tiempo de devolver un error estructurado antes de que el cliente corte la llamada.

> [!IMPORTANT]
> **No hay ningún resolutor automático de captcha en navaja.** La imagen del captcha **nunca** se envía a un modelo de visión, a un servicio de OCR ni a ningún tercero. Una persona lee la imagen y envía la respuesta por el formulario local.

El desafío se queda **pegado a la sesión**: una vez resuelto, las siguientes peticiones de texto completo en la misma sesión normalmente no vuelven a preguntar. En la práctica esto significa aproximadamente **una resolución de captcha por sesión de investigación**, no una por resolución.

---

## 📦 Descargas por lotes

> [!IMPORTANT]
> **Respeta las condiciones de uso del CENDOJ.** El [aviso legal del CGPJ](https://www.poderjudicial.es/sede/es/Ayuda/Aviso-Legal) permite consultar la base de datos de jurisprudencia **solo para uso particular** y dice expresamente: *«No está permitida la utilización de la base de datos para usos comerciales, ni la descarga masiva de información»*.
>
> - Usa los lotes para reunir **unas pocas resoluciones concretas** que ya has elegido tras buscar, no para volcar resultados de búsqueda enteros ni para rastrear la base de datos.
> - El tope de **100 URLs por lote** es un límite técnico de navaja, **no un permiso** del sitio para descargar esa cantidad.
> - Mantén `NAVAJA_MAX_CONCURRENTES` en `1` y no encadenes lotes seguidos.
> - Construir una base de datos propia, redistribuir resoluciones o darles un uso comercial es **reutilización**, y requiere seguir el procedimiento del CENDOJ ([Reglamento 3/2010 del CGPJ](https://www.boe.es/buscar/doc.php?id=BOE-A-2010-17860)).
>
> El aviso legal advierte de que cualquier uso contrario *«podrá dar lugar a la adopción de las medidas legales que procedan»*. Esta nota resume las condiciones publicadas; no es asesoramiento jurídico.

Para más de un documento, usa el flujo de tres pasos en lugar de muchas llamadas bloqueantes:

### 1️⃣ Arrancar

```python
iniciar_descargas(urls)
# → { ok, batch_id, jobs, captcha_url, max_concurrentes }
```

Devuelve de inmediato. `jobs` trae un registro `{job_id, url, state}` por cada URL, en orden de envío. La URL del captcha está disponible **antes de cualquier espera**: una persona puede abrirla una vez y dejarla abierta mientras el lote se vacía.

### 2️⃣ Consultar

```python
estado_descargas(batch_id)
```

Sondea hasta que los trabajos lleguen a un estado terminal. Si omites `batch_id`, devuelve el lote más reciente. Un `batch_id` desconocido devuelve una lista `jobs` **vacía**, no un error.

El resultado son **solo metadatos** —id de trabajo, URL, estado, intentos, ruta del PDF y código de error—, así que sondear un lote grande es barato.

### 3️⃣ Recoger

```python
recoger_descarga(job_id)
```

La llamada **no es destructiva**: volver a llamarla con el mismo `job_id` devuelve la misma carga útil. Y esa carga tiene exactamente la misma forma que la de `ver_texto_completo`, así que el mismo código puede manejar ambos caminos.

### 4️⃣ Cancelar (si algo se traba)

```python
cancelar_lote(batch_id)
# → { ok, batch_id, cancelled, already_finished, left_running }
```

Si el sitio pide un captcha que nadie va a responder, los trabajos del lote pueden quedar esperando hasta `captcha_timeout` (300 s por documento por defecto). `cancelar_lote` corta esa espera: los trabajos aún en cola pasan a `cancelled` al instante, y el que ya está descargando se deja terminar (su resultado queda en `recoger_descarga` como siempre). Llamarlo dos veces es seguro y reporta los mismos conteos.

Un trabajo cancelado devuelve `ok: false` con `error_code: "cancelled"` — misma forma que un fallo de descarga, así que un `estado_descargas` después del corte muestra el estado real de cada trabajo y el mismo código puede manejar cancelación y fallo.

### Límites y validación

Un lote admite **como máximo 100 URLs**, y todas se validan por adelantado. **Una llamada rechazada no encola nada.**

| `error_code` | Motivo |
| --- | --- |
| `empty_urls` | Lista vacía |
| `too_many_urls` | Más de 100 URLs |
| `invalid_url` | Primera URL no interpretable, nombrada en `error` |

El registro guarda los **10 lotes más recientes**; los lotes terminados más antiguos se podan automáticamente, y sus ids de trabajo pasan a leerse como `unknown_job`. **Un lote con trabajos en cola o en ejecución nunca se poda.**

### Estados de los trabajos

`queued` → `running` → `done` | `failed`

> [!WARNING]
> Un estado `done` significa que el ejecutor **retornó**, no que la descarga saliera bien. Un documento que falló al descargarse —por ejemplo, con una URL inválida— **también acaba en `done`**, con `ok: false` y un `error_code`. Solo una excepción inesperada que se escape del ejecutor produce `failed`.
>
> Quien llama debe inspeccionar `ok` y `error_code` de cada trabajo, y **no debe tratar `done` como éxito**.

### Concurrencia

El número de hilos sale de `NAVAJA_MAX_CONCURRENTES` y vale `1` por defecto.

Ese valor por defecto es **una decisión de producto de este proyecto, no un límite publicado por el sitio**: el repositorio no contiene ninguna cuota numérica de CENDOJ, ni manejo de `Retry-After`, ni código de backoff. Como el captcha se queda pegado a la sesión, un desafío resuelto suele servir para todo el lote, así que un solo hilo es el valor conservador.

---

## 💾 Guardado de PDF

Cuando la respuesta final es un PDF, navaja **guarda el fichero en disco además de devolver el texto extraído**. Vale tanto para `ver_texto_completo` como para `navaja-doc`; no hay que activarlo en cada llamada.

El directorio de destino se resuelve en este orden:

1. `NAVAJA_PDF_DIR`, si está definida.
2. `$XDG_DATA_HOME/navaja/pdfs`, si `XDG_DATA_HOME` está definida.
3. `~/.local/share/navaja/pdfs` en caso contrario.

navaja crea el directorio cuando hace falta.

El nombre del fichero se toma del parámetro `name=` que el servidor envía en la cabecera `Content-Type`, por ejemplo `name="STS_3679_2026.pdf"`. Ese valor se sanea hasta un nombre base seguro antes de llegar al sistema de ficheros. Si la cabecera no trae un nombre usable, o el nombre sería inseguro o demasiado largo, navaja intenta primero extraer el ROJ del propio texto de la resolución y usarlo como nombre; si tampoco puede, recurre al nombre determinista construido desde la URL del documento: `<referencia>_<optimize>.pdf`.

El campo `pdf_save_reason` dice qué regla se aplicó (`server_sent_name`, `missing_name`, `unsafe_name`, `overlong_name`, `identical_bytes`, ...).

Si la respuesta no es un PDF, no se escribe nada y `pdf_save_reason` vale `not_pdf`.

> [!NOTE]
> Un fallo de escritura (permisos, disco lleno, `NAVAJA_PDF_DIR` incorrecta) **nunca hace fracasar la descarga**: `ok` sigue siendo `True`, el texto completo se devuelve igual, y `pdf_save_error` explica qué pasó.

El resultado de `ver_texto_completo` siempre incluye tres claves extra:

| Clave | Contenido |
| --- | --- |
| `pdf_path` | Ruta del fichero guardado, o `None` |
| `pdf_save_reason` | Por qué tiene ese nombre, o por qué no se escribió nada |
| `pdf_save_error` | `None` si fue bien; si no, un mensaje legible |

---

## 🪄 Valores por defecto sin configurar nada

Por defecto navaja intenta que el formulario del captcha sea alcanzable sin configuración manual.

**Host** — `navaja-doc` y `navaja-mcp` detectan la interfaz en este orden:

1. `NAVAJA_CAPTCHA_HOST`, si está definida.
2. La dirección IPv4 de la interfaz `tailscale0`, si existe.
3. `127.0.0.1`.

**Token** — el token de la ruta del captcha es estable entre ejecuciones. Se lee de `NAVAJA_CAPTCHA_TOKEN` si está definida, y si no de `$XDG_STATE_HOME/navaja/captcha-token` (por defecto `~/.local/state/navaja/captcha-token`). Si no existe ninguno, se genera con `secrets.token_urlsafe(32)` y se persiste. El directorio de estado se crea con permisos `0700` y el fichero con `0600`; la escritura es atómica, así que dos ejecuciones simultáneas no pueden dejarlo a medias.

El host elegido se anuncia por stderr junto con la URL del formulario, así que **nunca es una exposición silenciosa**.

---

## 💻 Funciona sin pantalla

navaja **no abre ningún navegador** en la máquina que ejecuta el servidor. El formulario local es un servidor HTTP diminuto, y la persona llega a él desde cualquier dispositivo que pueda conectarse a esa interfaz.

Está pensado a propósito para un VPS u otro equipo sin pantalla. Puedes ejecutar `navaja-mcp` en un servidor sin monitor y resolver el captcha por un túnel SSH. Si el formulario se sirve en `127.0.0.1:8765` en la máquina remota:

```bash
ssh -L 8765:127.0.0.1:8765 <tu-vps>
```

Luego abre `http://127.0.0.1:8765/<token>/` en local.

> [!TIP]
> Esto está verificado de punta a punta: se resolvió un captcha real de CENDOJ por un túnel SSH y el servidor devolvió el PDF al primer intento. Como el túnel va por el puerto 22, que los cortafuegos suelen permitir, no hizo falta tocar `ufw`.

---

## 🔧 Configuración

| Variable | Por defecto | Para qué sirve |
| --- | --- | --- |
| `NAVAJA_CAPTCHA_HOST` | autodetección | Interfaz a la que se ata el formulario local. Sin definir, navaja toma la IPv4 de `tailscale0` y recurre a `127.0.0.1`. |
| `NAVAJA_CAPTCHA_PORT` | `8765` | Puerto de escucha del formulario local. |
| `NAVAJA_CAPTCHA_TOKEN` | persistido | Token estable de la ruta. Sin definir, navaja lee o crea `$XDG_STATE_HOME/navaja/captcha-token`. Mínimo 16 caracteres, solo `A-Z`, `a-z`, `0-9`, `-`, `_`. |
| `NAVAJA_MAX_CONCURRENTES` | `1` | Hilos de trabajo de la cola de descargas. Decisión de producto de este proyecto, no un límite publicado por el sitio. |
| `NAVAJA_PDF_DIR` | `~/.local/share/navaja/pdfs` | Directorio donde se guardan los PDFs. Recurre a `$XDG_DATA_HOME/navaja/pdfs` si `XDG_DATA_HOME` está definida. |

En Windows, si otra instancia de navaja ya está escuchando en el mismo puerto, el segundo proceso falla con *address already in use*; mata la instancia anterior o cambia `NAVAJA_CAPTCHA_PORT`.

> [!CAUTION]
> Si `NAVAJA_CAPTCHA_HOST` vale `0.0.0.0`, `::` o cadena vacía, **el servidor se niega a arrancar**. Ata una interfaz concreta.

---

## 🚀 Instalación

navaja es un programa local que tu chat (Claude, ChatGPT, Cursor…) lanza en tu propia máquina y al que se conecta por el protocolo MCP. Hay cuatro caminos, ordenados de menos a más técnico. Si usas **Claude Desktop en macOS o Windows**, el paquete `.mcpb` es un doble clic.

### Opción A — Un clic con el paquete `.mcpb` (Claude Desktop)

1. Descarga `navaja-<versión>.mcpb` desde la sección [Releases](https://github.com/jgarcialaneitor/navaja/releases) del repositorio.
2. Haz **doble clic** en el archivo (o arrástralo a la ventana de Claude Desktop). Aparece el diálogo de instalación con las herramientas que trae.
3. Acepta. Reinicia Claude por completo (menú ☰ → Quit / Salir) si te lo pide.
4. Comprueba con: *¿Qué herramientas de navaja tienes disponibles?*

El paquete no necesita Python ni nada preinstalado: Claude Desktop gestiona el runtime y las dependencias la primera vez que arranca el servidor (unos segundos la primera vez). Para regenerar el paquete desde el repositorio: `uv run python scripts/build_mcpb.py --out build/mcpb` y luego `npx @anthropic-ai/mcpb pack build/mcpb dist/navaja.mcpb`.

> [!NOTE]
> El doble clic instala el servidor; el **captcha sigue siendo humano** por diseño (no hay resolución automática). La primera descarga de texto completo te pedirá abrir la URL del formulario en el navegador — se explica más abajo.

### Requisito único: `uv`

navaja no necesita que instales Python: `uv` lo baja solo, junto con las dependencias, la primera vez que lo ejecuta. Instálalo una vez:

```bash
# macOS y Linux
curl -LsSf https://astral.sh/uv/install.sh | sh
```

```powershell
# Windows (PowerShell)
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

Cierra y vuelve a abrir la terminal después de instalarlo. Nada más: no hay que clonar el repositorio ni instalar nada por separado.

### Opción B — Pídeselo a tu asistente

Si tu asistente **puede ejecutar comandos** (Claude Code, Codex CLI, la app de ChatGPT de escritorio), no necesitas tocar ninguna configuración: copia y pega esto tal cual:

```text
Instala el servidor MCP "navaja" para este cliente. El proyecto está en
https://github.com/jgarcialaneitor/navaja

1. Si falta `uv`, instálalo con su instalador oficial.
2. Registra el servidor en la configuración MCP de este cliente con:
   command: uvx
   args: --from git+https://github.com/jgarcialaneitor/navaja navaja-mcp
3. Dime cómo reiniciar el cliente para que lo cargue.
4. Después de reiniciar, lista las herramientas de navaja para verificar.
```

> [!IMPORTANT]
> En el **chat de Claude Desktop** el asistente **no** puede editar su propia configuración ni ejecutar comandos en tu máquina: te dirá paso a paso qué hacer, pero los pasos los haces tú (opción C). Para que el asistente instale navaja sin ayuda, usa Claude Code, Codex CLI o la app de ChatGPT de escritorio.

### Opción C — Manual en Claude Desktop

1. Abre Claude Desktop → menú ☰ → **File → Settings → Developer** (macOS) o **Configuración → Desarrollador** (Windows).
2. Pulsa **Edit Config** y abre el fichero `claude_desktop_config.json` que se señala:
   - macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
   - Windows: `%APPDATA%\Claude\claude_desktop_config.json`
3. Añade esta entrada dentro de `mcpServers` (o créala si no existe):

```json
{
  "mcpServers": {
    "navaja": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/jgarcialaneitor/navaja",
        "navaja-mcp"
      ]
    }
  }
}
```

4. Guarda y **cierra Claude por completo** (menú ☰ → Quit / Salir), no solo la ventana. Al reabrirlo, el icono de herramientas (🔨/🧩) del cuadro de mensaje debe mostrar siete herramientas de `navaja`.

Con esta entrada el captcha se sirve en `127.0.0.1`, lo correcto cuando el chat y el navegador están en la misma máquina.

<details>
<summary>Servidor en otra máquina (VPS) o con Tailscale</summary>

Si el servidor corre en un equipo distinto del que abre el navegador (por ejemplo, un VPS), usa el comando clonado y fija el host del formulario:

```json
{
  "mcpServers": {
    "navaja": {
      "command": "uv",
      "args": [
        "--directory",
        "/ruta/a/tu/clon/navaja",
        "run",
        "navaja-mcp"
      ],
      "env": {
        "NAVAJA_CAPTCHA_HOST": "<tu-ip-de-tailnet>",
        "NAVAJA_CAPTCHA_PORT": "8765",
        "NAVAJA_CAPTCHA_TOKEN": "<tu-token-estable-de-16-caracteres-o-mas>"
      }
    }
  }
}
```

`NAVAJA_CAPTCHA_TOKEN` es opcional, pero se recomienda un token estable cuando el servidor se ata a una dirección que no es loopback. Sustituye `<tu-ip-de-tailnet>` por tu IP real de Tailscale. Las [reglas de seguridad](#-reglas-de-seguridad) aplican igual.

</details>

### Opción D — ChatGPT de escritorio

La app de ChatGPT de escritorio soporta servidores MCP **locales** (motor Codex). El ChatGPT web **no**: ahí solo se pueden conectar servidores remotos, y navaja es un programa de tu máquina.

En la app: **Settings → MCP servers → Add server**, tipo **STDIO**, y rellena:

| Campo | Valor |
| --- | --- |
| Comando | `uvx` |
| Argumentos | `--from git+https://github.com/jgarcialaneitor/navaja navaja-mcp` |

O edita directamente `~/.codex/config.toml`:

```toml
[mcp_servers.navaja]
command = "uvx"
args = ["--from", "git+https://github.com/jgarcialaneitor/navaja", "navaja-mcp"]
```

Escribe `/mcp` en el cuadro de chat para ver los servidores conectados. El soporte de MCP local de ChatGPT es más reciente que el de Claude; si las herramientas no terminan de aparecer, usa Claude Desktop o Claude Code.

### Comprobar que quedó bien

Tras reiniciar el cliente, pídele al asistente:

```text
¿Qué herramientas de navaja tienes disponibles?
```

Deben aparecer las siete (`buscar_sentencias`, `ver_texto_completo`, …). Luego prueba una búsqueda real —no dispara captcha—:

```text
Busca en navaja sentencias sobre "cláusulas abusivas", muestra 10 y resúmelas.
```

### Primera descarga de texto completo (el captcha)

La primera vez que pidas el texto completo de una resolución, puede aparecer el captcha del sitio. El asistente te dará una **URL local del formulario** (viene en `captcha_url` de `estado_servidor` o del error): ábrela en tu navegador, escribe los caracteres de la imagen y deja la pestaña abierta para los siguientes desafíos. Los detalles están en [📄 Cómo funciona la descarga de texto completo](#-cómo-funciona-la-descarga-de-texto-completo).

> [!NOTE]
> Instalación de **un solo clic** para Claude Desktop: usa la **opción A** (paquete `.mcpb`). Con `uvx` el registro queda en dos campos fijos, así que las opciones de arriba siguen valiendo para otros clientes.

---

## 🧰 Uso suelto, sin MCP

Descarga un único documento desde la línea de comandos con `navaja-doc`:

```bash
navaja-doc "https://www.poderjudicial.es/search/AN/openDocument/<hash-hex-de-16-o-32>/<AAAAMMDD>"
```

Si el puerto por defecto ya lo tiene una sesión de `navaja-mcp`, `navaja-doc` falla rápido con un mensaje accionable en vez de hacer primero un viaje a CENDOJ. Usa `--port 0` para que el sistema elija un puerto libre; la URL real del formulario se anuncia por stderr.

Para conseguir una URL de documento real ahora mismo:

```bash
uv run python -c \
  "from navaja.cendoj import CendojClient; c=CendojClient(); print(c.search('clausulas abusivas').as_dict()['results'][0]['url_documento']); c.close()"
```

Luego pega esa URL en `navaja-doc`.

---

## 🔒 Reglas de seguridad

El formulario local del captcha es una pequeña superficie web. Trátala con cuidado.

**1. Nunca ates `0.0.0.0`.** El código lo rechaza. Ata solo una interfaz concreta: `127.0.0.1`, una IP de Tailscale, u otra dirección privada.

**2. Mantén el formulario en una red privada** o llega a él por un túnel SSH. No lo expongas a internet.

**3. Si usas Tailscale:** usa `tailscale serve`, **nunca** `tailscale funnel`. `funnel` publica el servicio en internet; `serve` se queda en tu tailnet.

> [!CAUTION]
> Estas reglas existen porque **el propio equipo de este proyecto fue comprometido** a través de un puerto expuesto. Atar de forma estrecha no es aquí una formalidad.

---

## 🧪 Desarrollo

```bash
uv sync
uv run pytest                          # determinista, contra fixtures guardados
NAVAJA_LIVE=1 uv run pytest -m live    # opcional: golpea el sitio real
```

**Suite actual: `367 passed, 2 skipped`.**

Los tests **nunca** tocan el sitio real salvo que definas `NAVAJA_LIVE=1`.

---

## ✅ Verificado

El viaje completo con captcha real de CENDOJ está verificado de punta a punta:

```bash
uv run navaja-doc "https://www.poderjudicial.es/search/AN/openDocument/3fb62a5395c8aaa1a0a8778d75e36f0d/20260917"
```

Salida:

```
Fetching full text for https://www.poderjudicial.es/search/AN/openDocument/3fb62a5395c8aaa1a0a8778d75e36f0d/20260917
Captcha form will bind to 127.0.0.1 because no tailscale0 interface found
Captcha form binding to 127.0.0.1; ready at http://127.0.0.1:8765/<token>/
Result: success
Attempts: 1
Content-Type: application/pdf; name="STS_3679_2026.pdf"
Text preview: JURISPRUDENCIA Roj: STS 3679/2026 - ECLI:ES:TS:2026:3679 Id Cendoj: 28079110012026101379
Órgano: Tribunal Supremo. Sala de lo Civil Sede: Madrid Sección: 1 Fecha: 10/09/2026
Nº de Recurso: 288/2022 Nº de Resolución: 1413/2026 Procedimiento: Recurso de casación
Ponente: PEDRO JOSE VELA TORRES Tipo de Resolución: Sentencia
```

Qué demuestra esto:

- Una respuesta humana correcta al captcha `stickyImg` del sitio **devuelve el PDF real**. El circuito completo funciona.
- Funcionó **al primer intento**, usando solo el cuerpo POST del captcha que ya existía. No hicieron falta cookies extra, cabecera `Referer` ni baile de reintentos.
- El flujo de formulario local + túnel SSH **funciona en un VPS sin pantalla**.
- `pypdf` extrae texto limpio del PDF devuelto.
- Los metadatos del PDF son **más ricos** que los que produce el parser de resultados de búsqueda. El documento lleva `Órgano`, `Sede`, `Sección`, `Fecha`, `Nº de Recurso`, `Nº de Resolución`, `Procedimiento`, `Ponente`, `Tipo de Resolución` e `Id Cendoj`. La página de resultados ya publica `Municipio: Madrid`; el dato que el parser descartaba era la `Sala de lo Civil`, que ahora se expone como `sala`. `sede` sigue valiendo `null` para el Tribunal Supremo porque ni el título ni el ROJ llevan código de sede en esas resoluciones.

Esta verificación se hizo con `navaja-doc` directamente, no a través de un cliente MCP.

---

## 🌹 Agradecimientos

<div align="center">

<img src="docs/img/gentle-ai-homenaje.jpg" alt="Built with Gentle-AI — AI-powered software" width="600">

</div>

navaja se construyó con [Gentle AI](https://github.com/Gentleman-Programming/gentle-ai), de [Gentleman Programming](https://github.com/Gentleman-Programming). Sin su harness y su forma de trabajar, este proyecto no habría pasado de idea a realidad. Gracias.
