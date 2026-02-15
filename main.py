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
from misc import Power 

# ==========================================
# CONFIGURACOES
# ==========================================
VERSAO_ATUAL = "2.3" # V22 - Autocura de Rede
INTERVALO_ENVIO = 60 

# MQTT
MQTT_BROKER = "broker.hivemq.com"
MQTT_TOPIC_DADOS = "infratron/v1/dados"
DEVICE_ID = "Infratron_001"

# OTA
HOST_GITHUB = "raw.githubusercontent.com"
PATH_VERSAO = "/vrasd2/MTECH/main/versao.txt"
PATH_FIRMWARE = "/vrasd2/MTECH/main/main.py"
OTA_CHECK_FREQ = 1 

# Arquivos
DIR_ROOT = "/usr/"
ARQUIVO_BUFFER = DIR_ROOT + "buffer.txt"
ARQUIVO_TEMP = DIR_ROOT + "temp.txt"
ARQUIVO_NOVO = DIR_ROOT + "main_novo.py"

# ==========================================
# 1. SETUP & TOOLS
# ==========================================
def boot_check():
    print("--- BOOT V22 (AUTOCURA) ---")
    utime.sleep(2)

def forcar_reset():
    print("[SYS] INICIANDO RESET...")
    utime.sleep(2)
    try: Power.powerRestart()
    except: pass
    utime.sleep(2)
    try: machine.reset()
    except: pass
    while True: pass

# ==========================================
# 2. GESTAO DE REDE AVANCADA (AUTOCURA)
# ==========================================
def check_net():
    try:
        i = dataCall.getInfo(1,0)
        if isinstance(i,tuple) and len(i)>2 and isinstance(i[2],list) and len(i[2])>2:
            ip = i[2][2]
            return ip != '0.0.0.0' and ip != ''
    except: pass
    return False

def reparar_conexao_nuclear():
    """
    Protocolo de Autocura:
    Derruba o sinal de radio (Modo Aviao) e sobe novamente.
    Isso força a torre a limpar a sessao e entregar novo IP.
    """
    print("[REDE] !!! ATIVANDO PROTOCOLO DE AUTOCURA !!!")
    try:
        print("[REDE] Modo Aviao ON...")
        net.setModemFun(0) 
        utime.sleep(10) # Espera 10s para garantir desconexao total
        
        print("[REDE] Modo Aviao OFF (Buscando Torre)...")
        net.setModemFun(1)
        utime.sleep(10) # Tempo para registrar na torre
        
        print("[REDE] Ativando Dados...")
        dataCall.activate(1)
        utime.sleep(5)
        
        if check_net():
            print("[REDE] RECUPERADO COM SUCESSO!")
            return True
        else:
            print("[REDE] Ainda sem IP. Tentando novamente no proximo ciclo.")
    except Exception as e:
        print("[REDE] Erro Autocura: " + str(e))
    return False

# ==========================================
# 3. HTTP RAW TOOL
# ==========================================
def http_get_raw_save(host, path, dest_file):
    s = None
    try:
        print("[RAW] Connect: " + host)
        addr = usocket.getaddrinfo(host, 443)[0][-1]
        s = usocket.socket(usocket.AF_INET, usocket.SOCK_STREAM)
        s.connect(addr)
        ssl_s = ussl.wrap_socket(s, server_hostname=host)
        req = "GET {} HTTP/1.1\r\nHost: {}\r\nConnection: close\r\nUser-Agent: QPY\r\n\r\n".format(path, host)
        ssl_s.write(req.encode())
        print("[RAW] Baixando...")
        header_finished = False
        buffer = b""
        with open(dest_file, 'w') as f:
            while True:
                chunk = ssl_s.read(512) 
                if not chunk: break
                if not header_finished:
                    buffer += chunk
                    idx = buffer.find(b"\r\n\r\n")
                    if idx >= 0:
                        header_finished = True
                        f.write(buffer[idx+4:])
                        buffer = b""
                else: f.write(chunk)
        ssl_s.close(); s.close()
        return True
    except:
        if s: 
            try: s.close()
            except: pass
        return False

def http_get_version(host, path):
    s = None
    try:
        addr = usocket.getaddrinfo(host, 443)[0][-1]
        s = usocket.socket(usocket.AF_INET, usocket.SOCK_STREAM)
        s.connect(addr)
        ssl_s = ussl.wrap_socket(s, server_hostname=host)
        req = "GET {} HTTP/1.1\r\nHost: {}\r\nConnection: close\r\nUser-Agent: QPY\r\n\r\n".format(path, host)
        ssl_s.write(req.encode())
        resp = b""
        while True:
            data = ssl_s.read(128)
            if not data: break
            resp += data
        ssl_s.close(); s.close()
        idx = resp.find(b"\r\n\r\n")
        if idx > 0: return resp[idx+4:].decode().strip()
        return ""
    except:
        if s: s.close()
        return ""

# ==========================================
# 4. BUFFER
# ==========================================
class DataBuffer:
    def _existe(self, path):
        try: os.stat(path); return True
        except: return False
    
    def salvar(self, payload):
        try:
            try:
                if os.stat(ARQUIVO_BUFFER)[6] > 30000: os.remove(ARQUIVO_BUFFER)
            except: pass
            with open(ARQUIVO_BUFFER, 'a') as f:
                f.write(payload + "\n")
            print("[BUF] Salvo.")
        except: pass

    def processar_fila(self, mqtt):
        if self._existe(ARQUIVO_TEMP): self._enviar(ARQUIVO_TEMP, mqtt)
        elif self._existe(ARQUIVO_BUFFER):
            try:
                os.rename(ARQUIVO_BUFFER, ARQUIVO_TEMP)
                self._enviar(ARQUIVO_TEMP, mqtt)
            except: pass

    def _enviar(self, arquivo, mqtt):
        print("[BUF] Enviando passados...")
        try:
            restantes = []
            with open(arquivo, 'r') as f: linhas = f.readlines()
            for linha in linhas:
                linha = linha.strip()
                if len(linha) < 10: continue
                try:
                    mqtt.publish(MQTT_TOPIC_DADOS, linha)
                    utime.sleep_ms(100)
                except:
                    restantes.append(linha); break 
            try: os.remove(arquivo)
            except: pass
            if len(restantes) > 0:
                with open(ARQUIVO_BUFFER, 'a') as f:
                    for l in restantes: f.write(l + "\n")
        except:
            try: os.remove(arquivo)
            except: pass

    def tem_dados(self): return self._existe(ARQUIVO_BUFFER)

# ==========================================
# 5. OTA MANAGER
# ==========================================
class OTAManager:
    def checar_e_atualizar(self):
        print("[OTA] Checando...")
        gc.collect()
        v_remota = http_get_version(HOST_GITHUB, PATH_VERSAO)
        print("[OTA] Web: [" + v_remota + "] Modulo: [" + VERSAO_ATUAL + "]")
        if v_remota != "" and v_remota != VERSAO_ATUAL:
            print("[OTA] ATUALIZANDO...")
            gc.collect()
            utime.sleep(1)
            if http_get_raw_save(HOST_GITHUB, PATH_FIRMWARE, ARQUIVO_NOVO):
                try:
                    sz = os.stat(ARQUIVO_NOVO)[6]
                    print("[OTA] Tamanho: " + str(sz))
                    if sz > 100:
                        print("[OTA] Aplicando...")
                        try: os.remove("/usr/main.py")
                        except: pass
                        os.rename(ARQUIVO_NOVO, "/usr/main.py")
                        print("[OTA] SUCESSO! RESETANDO...")
                        forcar_reset()
                except: pass
            print("[OTA] Falha Download.")
            try: os.remove(ARQUIVO_NOVO)
            except: pass

# ==========================================
# 6. DRIVERS
# ==========================================
class GPSDriver:
    def __init__(self):
        self.u = 1; self.baud = 9600; self.pin = 22
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
                    d = self.uart.read(self.uart.any())
                    try: buf += d.decode()
                    except: pass
                    while '\n' in buf:
                        l, buf = buf.split('\n', 1)
                        if "$GNGGA" in l:
                            p = l.strip().split(',')
                            if len(p)>7 and p[6]!='0':
                                return (self.cvt(p[2],p[3]), self.cvt(p[4],p[5]))
                except: pass
            utime.sleep_ms(200)
        return (0.0, 0.0)

class DS18B20:
    def __init__(self, u=2): self.u = u; self.uart = None
    def init(self, b):
        if self.uart: self.uart.close()
        self.uart = UART(self.u, b, 8, 0, 1, 0)
        utime.sleep_ms(10)
        while self.uart.any(): self.uart.read()
    def reset(self):
        try:
            self.init(9600); self.uart.write(b'\xF0'); utime.sleep_ms(5)
            if not self.uart.any(): return False
            return self.uart.read(1)[0] != 0xF0
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
            if self.uart.read(1)[0] == 0xFF: v |= (1<<i)
        return v
    def get(self):
        try:
            if not self.reset(): return None
            self.wb(0xCC); self.wb(0x44); self.uart.close(); utime.sleep_ms(800)
            if not self.reset(): return None
            self.wb(0xCC); self.wb(0xBE)
            l = self.rb(); m = self.rb(); self.uart.close()
            r = (m<<8)|l
            if r&0x8000: r = -((r^0xFFFF)+1)
            t = r/16.0
            return None if t==85.0 else t
        except: return None

def get_bat():
    try:
        r = bytearray(64); atcmd.sendSync('AT+CBC\r\n', r, '', 2)
        p = r.decode().split(',')
        if len(p)>=3: return int("".join([c for c in p[2] if c.isdigit()]))
    except: pass
    return 0

# ==========================================
# 7. MAIN LOOP
# ==========================================
def main_loop():
    boot_check()
    pm.autosleep(1)
    gps = GPSDriver()
    sensor = DS18B20()
    buf = DataBuffer()
    ota = OTAManager()
    
    # Inicializacao Padrao
    try: net.setModemFun(1); utime.sleep(5)
    except: pass
    
    ota_cnt = 0
    falhas_rede = 0 # Contador de falhas (3 strikes)

    while True:
        try:
            t0 = utime.time()
            gc.collect()
            print("\n>>> CICLO V22 <<<")
            
            # 1. SENSORES
            print("1. Leitura...")
            temp = sensor.get() or 0.0
            bat = get_bat()
            
            gps.ligar()
            lat, lon = gps.fix(45)
            gps.desligar()
            
            pl = '{"id":"' + DEVICE_ID + '","ver":"' + VERSAO_ATUAL + '",'
            pl += '"ts":' + str(utime.time()) + ','
            pl += '"temp":' + str(temp) + ','
            pl += '"bat":' + str(bat) + ','
            pl += '"lat":' + str(lat) + ','
            pl += '"lon":' + str(lon) + '}'
            print("   " + pl)

            # 2. REDE E AUTOCURA
            if not check_net():
                print("   Sem IP. Tentando ativar...")
                try: dataCall.activate(1); utime.sleep(2)
                except: pass
            
            rede_ok = check_net()
            
            if not rede_ok:
                falhas_rede += 1
                print("   [ALERTA] Falha de Rede #" + str(falhas_rede))
                
                # Se falhar 3 vezes consecutivas (3 min), ativa o nuclear
                if falhas_rede >= 3:
                    reparar_conexao_nuclear()
                    falhas_rede = 0 # Reseta contador
            else:
                falhas_rede = 0 # Rede estavel, zera contador
            
            if rede_ok:
                # 3. OTA
                ota_cnt += 1
                if ota_cnt >= OTA_CHECK_FREQ:
                    ota_cnt = 0
                    ota.checar_e_atualizar()
                
                # 4. MQTT
                client = None
                try:
                    print("2. MQTT...")
                    client = MQTTClient(DEVICE_ID, MQTT_BROKER, keepalive=60)
                    client.connect()
                    if buf.tem_dados(): buf.processar_fila(client)
                    client.publish(MQTT_TOPIC_DADOS, pl)
                    print("   >> SUCESSO!")
                except Exception as e:
                    print("   Erro MQTT: " + str(e))
                    buf.salvar(pl)
                finally:
                    if client:
                        try: client.disconnect(); client.close()
                        except: pass
            else:
                print("   Offline. Bufferizando.")
                buf.salvar(pl)

            # 5. SLEEP
            tsleep = INTERVALO_ENVIO - (utime.time() - t0)
            if tsleep < 5: tsleep = 5
            print("Dormindo " + str(tsleep) + "s...")
            utime.sleep(tsleep)

        except Exception as e:
            print("FATAL: " + str(e))
            utime.sleep(10)

if __name__ == '__main__':
    main_loop()