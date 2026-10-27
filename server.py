import os, re, json, time, sqlite3
from functools import wraps
from flask import Flask, request, jsonify, Response
from google import genai
from google.genai import types

MODELO = "gemini-3.8-flash"
MODELOS = [MODELO, "gemini-flash-latest"]   # el segundo es respaldo
VENTANA_MIN = 10
DB = "yapeguard.db"
TOKEN = os.environ.get("YG_TOKEN", "")
CLAVE_DUENO = os.environ.get("YG_DUENO", "")
if not TOKEN or not CLAVE_DUENO:
    print("AVISO: falta YG_TOKEN o YG_DUENO. Sin ellos /pago y /dueno quedan bloqueados.")

client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
app = Flask(__name__)

def db():
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row; return c

with db() as c:
    c.execute("""CREATE TABLE IF NOT EXISTS pagos(
        id INTEGER PRIMARY KEY, nombre TEXT, monto REAL, codigo TEXT,
        recibido REAL, usado INTEGER DEFAULT 0)""")

def solo_dueno(fn):
    @wraps(fn)
    def w(*a, **k):
        au = request.authorization
        if not CLAVE_DUENO or not au or au.password != CLAVE_DUENO:
            return Response("Clave del dueño", 401, {"WWW-Authenticate": 'Basic realm="YapeGuard"'})
        return fn(*a, **k)
    return w

PROMPT = """Esta imagen puede ser una captura o una foto (con reflejos o ángulo) de un comprobante de Yape.
Responde SOLO un JSON con estas claves:
"legible": true/false (false si no puedes leer con seguridad el monto y el código),
"monto": número (ej. 0.1, 1, 33, 12.5) o null,
"nombre": texto o null,
"codigo": los 3 dígitos del "Código de seguridad" como texto, o null,
"nro_operacion": el número de operación como texto, o null.
No inventes datos: si algo no se ve, pon null."""

PANEL = """<div class=card><h3>__TITULO__</h3><div id=lista>Esperando pagos...</div></div>"""

HTML = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>YapeGuard</title>
<style>
body{font-family:system-ui,sans-serif;margin:0;background:#f4eefa;color:#2a1a3d}
header{background:#6a0087;color:#fff;padding:16px;font-size:20px;font-weight:700}
main{max-width:520px;margin:auto;padding:16px}
.card{background:#fff;border-radius:14px;padding:16px;margin-bottom:16px;box-shadow:0 2px 8px #0002}
h3{margin:0 0 10px}
.pago{display:flex;justify-content:space-between;padding:8px 0;border-bottom:1px solid #eee;font-size:15px}
.tag{font-size:12px;padding:2px 8px;border-radius:10px;background:#c8f7c5}
.tag.u{background:#ffe0b2}
button,label.b{display:block;width:100%;box-sizing:border-box;text-align:center;padding:14px;border:0;border-radius:10px;background:#00c2b3;color:#fff;font-size:17px;font-weight:700;margin-top:10px}
#prev{max-width:100%;max-height:220px;display:none;margin:10px auto;border-radius:8px}
#r{display:none;margin-top:12px;padding:20px;border-radius:12px;text-align:center}
#r b{font-size:26px}
#lista{max-height:320px;overflow-y:auto}
</style>
<header>YapeGuard</header>
<main>
__PANEL__
<div class=card style="background:#fff3b0"><b>Llama al dueño</b> si el resultado no es COINCIDE, si algo te parece raro o si el monto es alto.<br><a href="tel:+51999999999">📞 999 999 999</a></div>
<div class=card><h3>Verificar comprobante</h3>
<label class=b>Tomar foto o elegir captura<input type=file id=f accept="image/*" hidden></label>
<img id=prev><button id=bv>Verificar</button><div id=r></div></div>
</main>
<script>
const DUENO=__DUENO__;
const f=document.getElementById('f'),prev=document.getElementById('prev'),r=document.getElementById('r');
const esc=s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
f.onchange=()=>{if(f.files[0]){prev.src=URL.createObjectURL(f.files[0]);prev.style.display='block';r.style.display='none'}};
async function cargar(){
 try{
  const p=await (await fetch(DUENO?'/pagos':'/recientes')).json();
  const ahora=Date.now()/1000;
  document.getElementById('lista').innerHTML=p.map(x=>{
   const m=Math.round((ahora-x.recibido)/60);
   return '<div class=pago><span><b>S/ '+esc(x.monto)+'</b> · '+esc(x.nombre)+(DUENO?' · cód. '+esc(x.codigo):'')+'</span><span class="tag '+(x.usado?'u':'')+'">'+(x.usado?'verificado':'hace '+m+' min')+'</span></div>'}).join('')||'Esperando pagos...';
 }catch(e){}
}
setInterval(cargar,3000);cargar();
document.getElementById('bv').onclick=async()=>{
 if(!f.files[0]){alert('Elige una foto primero');return}
 r.style.display='block';r.style.background='#eee';r.textContent='Verificando...';
 const fd=new FormData();fd.append('img',f.files[0]);
 try{
  const j=await (await fetch('/verificar',{method:'POST',body:fd})).json();
  const col={'COINCIDE':'#c8f7c5','CÓDIGO YA USADO':'#ffe0b2','NO COINCIDE':'#ffcdd2','TOMA OTRA FOTO':'#fff3b0'};
  r.style.background=col[j.resultado]||'#eee';
  r.innerHTML='<b>'+esc(j.resultado)+'</b><br><small>'+esc(j.detalle)+'</small>';
  cargar();
 }catch(e){r.textContent='Error: '+e}
};
</script>"""

def pagina(dueno):
    titulo = "Todos los pagos reales" if dueno else "Últimos 6 pagos"
    return (HTML.replace("__PANEL__", PANEL).replace("__TITULO__", titulo)
                .replace("__DUENO__", "true" if dueno else "false"))

def leer(img, mime):
    ultimo = None
    for modelo in MODELOS:
        for intento in range(3):
            try:
                resp = client.models.generate_content(
                    model=modelo,
                    contents=[types.Part.from_bytes(data=img, mime_type=mime), PROMPT],
                    config=types.GenerateContentConfig(response_mime_type="application/json"))
                return json.loads(resp.text)
            except Exception as e:
                ultimo = e
                if "503" in str(e) or "429" in str(e):
                    time.sleep(2 * (intento + 1))
                    continue
                break   # otro error: pasa al siguiente modelo
    raise ultimo

@app.get("/")
def home():
    return pagina(False)          # empleado: solo foto y resultado

@app.get("/dueno")
@solo_dueno
def dueno():
    return pagina(True)           # dueño: además ve los pagos reales

@app.get("/recientes")
def recientes():
    with db() as c:
        return jsonify([{"monto": x["monto"], "nombre": x["nombre"], "recibido": x["recibido"], "usado": x["usado"]}
                        for x in c.execute("SELECT * FROM pagos ORDER BY id DESC LIMIT 6")])
    
@app.post("/pago")   # lo llama MacroDroid con el texto de la notificación
def pago():
    if not TOKEN or request.args.get("t") != TOKEN:
        return "no autorizado", 403
    txt = request.get_data(as_text=True).strip()
    m = re.search(r"(.+?)\s+te\s+envi\w*\s+un\s+pago\s+por\s+S/\s*(\d+(?:[.,]\d+)?).*?(\d{3})\s*$", txt, re.S)
    if not m:
        print("NO PARSEADO:", repr(txt)); return "formato desconocido", 400
    nombre, monto, codigo = m.group(1).strip(), float(m.group(2).replace(",", ".")), m.group(3)
    with db() as c:
        c.execute("INSERT INTO pagos(nombre,monto,codigo,recibido) VALUES(?,?,?,?)",
                  (nombre, monto, codigo, time.time()))
    print("PAGO REAL:", nombre, monto, codigo)
    return "ok"

@app.get("/pagos")
@solo_dueno
def pagos():
    with db() as c:
        return jsonify([dict(x) for x in c.execute("SELECT * FROM pagos ORDER BY id DESC LIMIT 200")])

@app.post("/verificar")
def verificar():
    f = request.files.get("img")
    if not f: return jsonify(resultado="TOMA OTRA FOTO", detalle="No llegó la imagen")
    try:
        d = leer(f.read(), f.mimetype or "image/jpeg")
    except Exception as e:
        return jsonify(resultado="ERROR DEL LECTOR", detalle=str(e))

    monto = d.get("monto")
    codigo = str(d.get("codigo") or "").strip()
    op = re.sub(r"\D", "", str(d.get("nro_operacion") or ""))
    if not re.fullmatch(r"\d{3}", codigo) and len(op) >= 3:
        codigo = op[-3:]   # el código son los últimos 3 dígitos del Nro. de operación
    if re.fullmatch(r"\d{3}", codigo) and len(op) >= 3 and op[-3:] != codigo:
        return jsonify(resultado="TOMA OTRA FOTO", detalle="El código y el Nro. de operación no concuerdan; repite la foto")
    if not d.get("legible") or monto is None or not re.fullmatch(r"\d{3}", codigo):
        return jsonify(resultado="TOMA OTRA FOTO", detalle="No se leyó bien el monto o el código")

    desde = time.time() - VENTANA_MIN * 60
    leido = f"Leído: S/ {monto}, código {codigo}"
    with db() as c:
        filas = c.execute("SELECT * FROM pagos WHERE codigo=? AND recibido>=?", (codigo, desde)).fetchall()
        buenas = [x for x in filas if abs(x["monto"] - float(monto)) < 0.001]
        if not buenas:
            return jsonify(resultado="NO COINCIDE", detalle=leido + f". Ningún pago real igual en los últimos {VENTANA_MIN} min. Llama al dueño.")
        for x in buenas:
            if not x["usado"]:
                cur = c.execute("UPDATE pagos SET usado=1 WHERE id=? AND usado=0", (x["id"],))
                if cur.rowcount == 1:
                    return jsonify(resultado="COINCIDE", detalle=leido + ". Igual a un pago real recibido.")
        return jsonify(resultado="CÓDIGO YA USADO", detalle=leido + ". Ese pago ya fue verificado antes (posible captura reciclada). Llama al dueño.")

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)