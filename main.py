import utime
import net
import dataCall
import uos as os
from machine import UART, Pin
import machine
from umqtt import MQTTClient
import pm
import atcmd 
import request 

# ==========================================
# CONFIGURACOES
# ==========================================
VERSAO_ATUAL = "1.3"
INTERVALO_ENVIO = 120

# MQTT (Apenas para dados)
MQTT_BROKER = "broker.hivemq.com"
MQTT_TOPIC_DADOS = "infratron/v1/dados"
DEVICE_ID = "Infratron_001"

# OTA (Atualizacao via HTTP)
# Cole aqui os links RAW dos seus arquivos
URL_VERSAO = "http://seusite.com/versao.txt"   # Arquivo contendo apenas "1.3"
URL_FIRMWARE = "http://seusite.com/main.py"    # O codigo novo

# Frequencia de Checagem OTA (Para economizar dados)
# 1 = Checa todo ciclo
# 10 = Checa a cada 10 ciclos (20 min)
OTA_CHECK_FREQ = 5 

# Arquivos Locais
ARQUIVO_BUFFER = "buffer.txt"
ARQUIVO_TEMP = "temp.txt"

# ==========================================
# 1. SETUP
# ==========================================
def boot_safety_delay():
    print("--- BOOT V13 (HTTP OTA) ---")
    utime.sleep(3)

# ==========================================
# 2. BUFFER
# ==========================================
class DataBuffer:
    def _existe(self, path):
        try: os.stat(path); return True
        except: return False
    
    def salvar(self, payload):
        try:
            # Limita tamanho (30KB)
            try:
                if os.stat(ARQUIVO_BUFFER)[6] > 30000: os.remove(ARQUIVO_BUFFER)
            except: pass
            
            with open(ARQUIVO_BUFFER, 'a') as f:
                f.write(payload + "\n")
            print("[BUF] Salvo Offline.")
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
# 3. OTA MANAGER (HTTP)
# ==========================================
class OTAManager:
    def checar_e_atualizar(self):
        print("[OTA] Checando versao no servidor...")
        try:
            # 1. Baixa o arquivo de texto com a versao
            r = request.get(URL_VERSAO)
            if r.status_code == 200:
                ver_remota = r.text.strip()
                r.close()
                print("[OTA] Remota: " + ver_remota + " | Local: " + VERSAO_ATUAL)
                
                # Compara strings. Se diferente, atualiza.
                if ver_remota != VERSAO_ATUAL:
                    print("[OTA] Nova versao encontrada! Baixando...")
                    return self._download_firmware()
                else:
                    print("[OTA] Sistema atualizado.")
            else:
                print("[OTA] Erro HTTP Versao: " + str(r.status_code))
                r.close()
        except Exception as e:
            print("[OTA] Erro Check: " + str(e))
        return False

    def _download_firmware(self):
        tmp = "main_novo.py"
        try:
            r = request.get(URL_FIRMWARE)
            if r.status_code == 200:
                with open(tmp, 'w') as f: f.write(r.text)
                r.close()
                
                # Validacao simples
                if os.stat(tmp)[6] < 100:
                    os.remove(tmp); return False
                
                print("[OTA] Download OK. Aplicando...")
                try: os.remove("main.py")
                except: pass
                os.rename(tmp, "main.py")
                utime.sleep(2)
                machine.reset() # Reinicia
                return True
            r.close()
        except:
            try: os.remove(tmp)
            except: pass
        return False

# ==========================================
# 4. DRIVERS
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

def check_net():
    try:
        i = dataCall.getInfo(1,0)
        if isinstance(i,tuple) and len(i)>2 and isinstance(i[2],list) and len(i[2])>2:
            return i[2][2] != '0.0.0.0'
    except: pass
    return False

# ==========================================
# 5. MAIN LOOP
# ==========================================
def main_loop():
    boot_safety_delay()
    
    pm.autosleep(1)
    gps = GPSDriver()
    sensor = DS18B20()
    buf = DataBuffer()
    ota = OTAManager()
    
    try: net.setModemFun(1); utime.sleep(5)
    except: pass
    
    contador_ota = 0

    while True:
        try:
            t0 = utime.time()
            print("\n>>> CICLO V13 <<<")
            
            # --- 1. SENSORES ---
            print("1. Lendo Sensores...")
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
            
            print("   Dado: " + pl)

            # --- 2. REDE ---
            if not check_net():
                try: dataCall.activate(1); utime.sleep(2)
                except: pass
            
            if check_net():
                # --- 3. OTA (HTTP POLLING) ---
                contador_ota += 1
                if contador_ota >= OTA_CHECK_FREQ:
                    contador_ota = 0
                    ota.checar_e_atualizar()
                
                # --- 4. MQTT (PUBLISH E TCHAU) ---
                client = None
                try:
                    print("2. MQTT Envio...")
                    client = MQTTClient(DEVICE_ID, MQTT_BROKER, keepalive=60)
                    # Nao setamos callback, nao damos subscribe
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
                print("   Sem Rede. Buffer.")
                buf.salvar(pl)

            # --- 5. SLEEP ---
            tsleep = INTERVALO_ENVIO - (utime.time() - t0)
            if tsleep < 5: tsleep = 5
            print("Dormindo " + str(tsleep) + "s...")
            utime.sleep(tsleep)

        except Exception as e:
            print("FATAL: " + str(e))
            utime.sleep(10)

if __name__ == '__main__':
    main_loop()