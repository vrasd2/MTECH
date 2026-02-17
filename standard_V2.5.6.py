import utime
import net
import dataCall
import uos as os
from machine import UART, Pin
import machine
from umqtt import MQTTClient
import pm
import atcmd 
import gc
import usocket
import ussl 
import ujson
import uselect
from misc import Power

# ==========================================
# 1. CONFIGURAÇÕES (MODO TESTE: 60s)
# ==========================================
VERSAO_ATUAL = "2.5.6" 
MQTT_BROKER = "broker.hivemq.com"
TOPICO_DADOS = "MTECH_SYSMO/v1/dados"
HOST_GITHUB = "raw.githubusercontent.com"
PATH_VERSAO_JSON = "/vrasd2/MTECH/main/versao.json"
DIR_ROOT = "/usr/"
ARQUIVO_PERFIL = DIR_ROOT + "perfil.json"
ARQUIVO_NOVO = DIR_ROOT + "main_novo.py"

def get_device_id():
    try:
        r = bytearray(64)
        atcmd.sendSync('AT+GSN\r\n', r, '', 2)
        clean = "".join([c for c in r.decode() if c.isdigit()])
        return "SM" + clean[-8:]
    except: return "SM_RECOVERY"

DEVICE_ID = get_device_id()

def forcar_reset():
    print("[SYS] REINICIANDO AGORA...")
    utime.sleep(2)
    try: Power.powerRestart()
    except: pass
    machine.reset()

def carregar_e_garantir_perfil():
    # TESTE: Intervalo padrao de 60s
    p = {"perfil": "standard", "intervalo": 60}
    try:
        if "perfil.json" not in os.listdir(DIR_ROOT):
            with open(ARQUIVO_PERFIL, "w") as f: ujson.dump(p, f)
            return p
        with open(ARQUIVO_PERFIL, "r") as f: return ujson.load(f)
    except: return p

# ==========================================
# 2. REDE E HTTP (ENGINE OTA)
# ==========================================
def check_net_real():
    try:
        info = dataCall.getInfo(1,0)
        if isinstance(info,tuple) and len(info)>2 and isinstance(info[2],list) and len(info[2])>2:
            ip = info[2][2]
            return ip != '0.0.0.0' and ip != ''
    except: pass
    return False

def reparar_conexao_nuclear():
    print("[REDE] !!! AUTOCURA !!!")
    try:
        net.setModemFun(0); utime.sleep(5); net.setModemFun(1)
        utime.sleep(15); dataCall.activate(1)
        return check_net_real()
    except: return False

def http_get_raw_save(host, path, dest_file):
    s = None
    try:
        gc.collect()
        addr = usocket.getaddrinfo(host, 443)[0][-1]
        s = usocket.socket(usocket.AF_INET, usocket.SOCK_STREAM)
        s.connect(addr)
        ssl_s = ussl.wrap_socket(s, server_hostname=host)
        req = "GET {} HTTP/1.1\r\nHost: {}\r\nConnection: close\r\nUser-Agent: QPY\r\n\r\n".format(path, host)
        ssl_s.write(req.encode())
        header_finished = False
        with open(dest_file, 'w') as f:
            while True:
                chunk = ssl_s.read(512)
                if not chunk: break
                if not header_finished:
                    idx = chunk.find(b"\r\n\r\n")
                    if idx >= 0:
                        header_finished = True
                        f.write(chunk[idx+4:])
                else: f.write(chunk)
        ssl_s.close(); s.close()
        return True
    except:
        if s: s.close()
        return False

class OTAManager:
    def _get_web_json(self):
        s = None
        try:
            gc.collect()
            addr = usocket.getaddrinfo(HOST_GITHUB, 443)[0][-1]
            s = usocket.socket(usocket.AF_INET, usocket.SOCK_STREAM)
            s.connect(addr)
            ssl_s = ussl.wrap_socket(s, server_hostname=HOST_GITHUB)
            req = "GET {} HTTP/1.1\r\nHost: {}\r\nConnection: close\r\n\r\n".format(PATH_VERSAO_JSON, HOST_GITHUB)
            ssl_s.write(req.encode())
            resp = b""
            while True:
                data = ssl_s.read(256)
                if not data: break
                resp += data
            ssl_s.close(); s.close()
            idx = resp.find(b"\r\n\r\n")
            if idx > 0: return ujson.loads(resp[idx+4:].decode().strip())
        except:
            if s: s.close()
        return None

    def executar(self, perfil_alvo):
        print("[OTA] Buscando firmware para perfil: {}".format(perfil_alvo))
        cfg = self._get_web_json()
        if not cfg or perfil_alvo not in cfg:
            print("[OTA] Erro: Perfil nao encontrado no GitHub.")
            return

        # Baixa FW do perfil alvo
        dados = cfg[perfil_alvo]
        url = dados.get("url")
        print("[OTA] Baixando: " + url)
        
        if http_get_raw_save(HOST_GITHUB, url, ARQUIVO_NOVO):
            try:
                if os.stat(ARQUIVO_NOVO)[6] > 500:
                    print("[OTA] Sucesso! Aplicando update...")
                    try: os.remove("/usr/main.py")
                    except: pass
                    os.rename(ARQUIVO_NOVO, "/usr/main.py")
                    # Nota: O reset acontece fora daqui
                    return True
            except: pass
        print("[OTA] Falha no download.")
        return False

# ==========================================
# 3. DRIVERS (CLAUSULA PETREA)
# ==========================================
class GPSDriver:
    def __init__(self):
        self.u, self.baud, self.pin = 1, 9600, 22
        self.pwr = Pin(self.pin, Pin.OUT, 0, 0); self.uart = None
    def ligar(self):
        self.pwr.write(1); utime.sleep(1)
        self.uart = UART(self.u, self.baud, 8, 0, 1, 0)
    def desligar(self):
        self.pwr.write(0)
        if self.uart: self.uart.close(); self.uart = None
    def cvt(self, v, d):
        if not v or '.' not in v: return 0.0
        try:
            i = v.find('.')
            dec = float(v[:i-2]) + (float(v[i-2:])/60.0)
            return -dec if d in ['S','W'] else dec
        except: return 0.0
    def fix(self, timeout):
        s = utime.time(); buf = ""
        while utime.time() - s < timeout:
            if self.uart and self.uart.any():
                try:
                    d = self.uart.read(self.uart.any()).decode()
                    buf += d
                    while '\n' in buf:
                        l, buf = buf.split('\n', 1)
                        if "$GNGGA" in l:
                            p = l.strip().split(',')
                            if len(p)>7 and p[6]!='0': return (self.cvt(p[2],p[3]), self.cvt(p[4],p[5]))
                except: pass
            utime.sleep_ms(200)
        return (0.0, 0.0)

class DS18B20:
    def __init__(self, u=2): self.u = u; self.uart = None
    def init(self, b):
        if self.uart: self.uart.close()
        self.uart = UART(self.u, b, 8, 0, 1, 0); utime.sleep_ms(10)
    def reset(self):
        try:
            self.init(9600); self.uart.write(b'\xF0'); utime.sleep_ms(5)
            return self.uart.read(1)[0] != 0xF0 if self.uart.any() else False
        except: return False
    def wb(self, b):
        self.init(115200)
        for i in range(8):
            self.uart.write(b'\xFF' if (b>>i)&1 else b'\x00')
            while not self.uart.any(): pass
            self.uart.read(1)
    def rb(self):
        self.init(115200); v = 0
        for i in range(8):
            self.uart.write(b'\xFF')
            while not self.uart.any(): pass
            if self.uart.any() and self.uart.read(1)[0] == 0xFF: v |= (1<<i)
        return v
    def get(self):
        try:
            if not self.reset(): return None
            self.wb(0xCC); self.wb(0x44); utime.sleep_ms(800)
            if not self.reset(): return None
            self.wb(0xCC); self.wb(0xBE)
            l, m = self.rb(), self.rb(); r = (m<<8)|l
            if r&0x8000: r = -((r^0xFFFF)+1)
            t = r/16.0
            return None if t == 85.0 else t
        except: return None

def get_bat():
    try:
        r = bytearray(64); atcmd.sendSync('AT+CBC\r\n', r, '', 2)
        p = r.decode().split(',')
        if len(p)>=3: return int("".join([c for c in p[2] if c.isdigit()]))
    except: pass
    return 0

# ==========================================
# 4. CALLBACK MQTT (CÉREBRO V25.6)
# ==========================================
def sub_cb(topic, msg):
    try:
        d = ujson.loads(msg)
        cmd = d.get("cmd")
        
        # --- COMANDO 1: MUDAR PERFIL E FORÇAR UPDATE ---
        if cmd == "set_profile":
            atual = carregar_e_garantir_perfil()
            novo_p = d.get("perfil", "standard")
            novo_i = d.get("intervalo", 60) # Default 60s no teste
            
            # Se o perfil for diferente, SALVA -> BAIXA FW -> RESET
            if novo_p != atual.get("perfil"):
                print("[CMD] Trocando Perfil: {} -> {}".format(atual["perfil"], novo_p))
                
                # 1. Salva Configuração
                with open(ARQUIVO_PERFIL, "w") as f:
                    ujson.dump({"perfil": novo_p, "intervalo": novo_i}, f)
                
                # 2. Baixa Firmware do Novo Perfil IMEDIATAMENTE
                ota = OTAManager()
                ota.executar(novo_p) # Tenta baixar, mesmo que falhe, o perfil já mudou
                
                # 3. Reseta
                forcar_reset()
            
            # Se perfil igual, mas tempo diferente, só salva e reseta (sem baixar FW)
            elif novo_i != atual.get("intervalo"):
                print("[CMD] Ajustando Intervalo...")
                with open(ARQUIVO_PERFIL, "w") as f:
                    ujson.dump({"perfil": novo_p, "intervalo": novo_i}, f)
                forcar_reset()

        # --- COMANDO 2: OTA EM MASSA (POR PERFIL) ---
        elif cmd == "ota":
            meu_perfil = carregar_e_garantir_perfil()["perfil"]
            alvo_perfil = d.get("perfil")
            alvo_versao = d.get("v")
            
            # SÓ OBEDECE SE O PERFIL FOR O MEU
            if alvo_perfil == meu_perfil:
                if alvo_versao != VERSAO_ATUAL:
                    print("[OTA] Nova versao {} para {}. Baixando...".format(alvo_versao, meu_perfil))
                    ota = OTAManager()
                    if ota.executar(meu_perfil):
                        forcar_reset()
                else:
                    print("[OTA] Versao ja instalada. Ignorando.")
            else:
                # Ignora silenciosamente (comando para outro grupo)
                pass

    except Exception as e:
        print("[ERRO CMD]", e)

# ==========================================
# 5. MAIN LOOP
# ==========================================
def main_loop():
    print("--- MTECH V25.6 (TESTE 60s) ---")
    pm.autosleep(1)
    gps, sensor = GPSDriver(), DS18B20()
    conf = carregar_e_garantir_perfil()

    while True:
        try:
            t0 = utime.time()
            gc.collect()
            print("\n>>> CICLO V25.6 | PERFIL: {} | ID: {} <<<".format(conf["perfil"], DEVICE_ID))
            
            # 1. Leitura
            temp = sensor.get() or 0.0
            bat = get_bat()
            gps.ligar(); lat, lon = gps.fix(40); gps.desligar()
            
            # Payload Oficial
            pl = ujson.dumps({
                "id": DEVICE_ID, "temp": temp, "ts": utime.time(), "bat": bat,
                "ver": VERSAO_ATUAL, "p": conf["perfil"], "lat": lat, "lon": lon
            })
            print("   Payload: " + pl)

            # 2. Rede e MQTT
            if not check_net_real(): dataCall.activate(1)
            
            if check_net_real():
                client = None
                try:
                    client = MQTTClient(DEVICE_ID, MQTT_BROKER, keepalive=60)
                    client.set_callback(sub_cb)
                    client.connect()
                    
                    client.subscribe("MTECH_SYSMO/v1/cmd/" + DEVICE_ID)
                    client.subscribe("MTECH_SYSMO/v1/cmd/todos")
                    client.publish(TOPICO_DADOS, pl)
                    
                    print("   [MQTT] Enviado. Aguardando Comandos (2s)...")
                    poller = uselect.poll()
                    poller.register(client.sock, uselect.POLLIN)
                    for _ in range(10): # 2 segundos
                        if poller.poll(200): client.check_msg()
                except: pass
                finally:
                    if client:
                        try: client.disconnect()
                        except: pass
            
            # 3. Sleep (60s no teste)
            esp = conf.get("intervalo", 60) - (utime.time() - t0)
            if esp < 10: esp = 10
            print("   Dormindo {}s...".format(esp))
            utime.sleep(esp)

        except Exception as e:
            print("ERRO FATAL:", e)
            utime.sleep(10)

if __name__ == '__main__':

    main_loop()
