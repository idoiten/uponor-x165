# CC1101 → ESP32: mottagning av Uponor Smatrix Wave-RF

Mål för den här fasen: enbart ta emot (speglar RTL-SDR-mottagaren), inte sända.
Hårdvaran (SZHJW CC1101 868MHz-modul) är beställd men inte inkopplad ännu.

## 1. Koppling (SPI)

CC1101-modulen har 2.0mm pin-pitch på SPI-sidan. Via adapterkortet
(2.0mm → 2.54mm) och JST PH2.0-kablarna kopplas den till ESP32:

| CC1101-pin | Funktion         | ESP32 (VSPI, standard) |
|------------|------------------|-------------------------|
| VCC        | 3.3V             | 3V3 (**inte** 5V!)      |
| GND        | GND              | GND                     |
| SI (MOSI)  | SPI data in      | GPIO23                  |
| SO (MISO)  | SPI data out     | GPIO19                  |
| SCK        | SPI clock        | GPIO18                  |
| CSN        | SPI chip select  | GPIO5                   |
| GDO0       | RX-avbrott (packet/sync) | GPIO4           |
| GDO2       | (valfri, ej nödvändig för RX) | lämna okopplad |

Anledningar/fallgropar:

- **3.3V, inte 5V.** CC1101-chippet tål inte 5V. Många ESP32-dev-kort har en
  5V-pinne (VIN/5V) bredvid 3V3 — dubbelkolla med multimeter innan ström på,
  särskilt eftersom adapterkortet gör det lätt att råka koppla fel rad.
- **Kort, tjocka kablar till VCC/GND.** CC1101 drar korta strömpulsar vid
  TX/kalibrering; långa tunna JST-kablar ger spänningsfall som yttrar sig som
  slumpartade paketfel. Håll strömkablarna så korta som praktiskt möjligt.
- **Antenn.** Om modulen inte har en fast helical/chip-antenn: en rak tråd på
  ~8.2 cm (kvartsvåg för 868MHz) i ANT-pinnen fungerar för bring-up. Byt till
  en riktig 868MHz-antenn (SMA-piska) när du vet att mottagningen funkar,
  annars blir räckvidden dålig.
- **GDO0 är den enda avbrottspinnen vi behöver** i mottagningsläget: den
  konfigureras (IOCFG0 = 0x06) till att gå hög när ett giltigt paket (CRC OK)
  har tagits emot, och gå låg igen när första byten läses ur RX-FIFO.
- Pinnarna ovan (GPIO23/19/18/5/4) är bara ett förslag som matchar ESP32:s
  hårdvaru-VSPI-bus rakt av — valfritt att byta i YAML:en om de krockar med
  annat du redan använder på kortet (t.ex. om du har markis-ESP32:n som
  referens, använd gärna samma pinnar där det går, för enhetlighet).

## 2. Varför just dessa register (sammanfattning)

Hämtat från det vi redan vet om protokollet (se `uponor_smatrix_wave_x165/`):

| Parameter        | Värde                          | Källa |
|-------------------|--------------------------------|-------|
| Bärfrekvens       | 868.25 MHz                     | `receiver.py --frequency` default (868_250_000) |
| Modulation        | 2-FSK/GFSK (chip provar GFSK först) | observerad tvåtons-FM i `dsp.py` |
| Baudrate          | ~38 378–38 382 baud            | `estimate_bitrate()` i loggar → matchar TI:s standardpreset "38.4 kBaud" nästan exakt |
| Frekvensavvikelse | ~16.5–20 kHz                   | tonseparation i loggarna (`robust_two_tones`) |
| Sync-ord          | `D3 91` upprepat (`D3 91 D3 91`)| `PREAMBLE_SYNC` i `protocol.py`, exakt CC1101:s "32-bit sync" (16-bitsordet skickat två gånger) |
| Preamble          | `AA AA AA AA` (4 byte)         | samma konstant |
| Längdbyte         | byte efter sync = antal payload-byte (exkl. CRC) | `declared = raw[8] + 11` i `protocol.py` |
| CRC               | CRC-16, poly 0x8005, init 0xFFFF, icke-reflekterad | `crc.py` → **identisk** med CC1101:s hårdvaru-CRC |

De två sista raderna är bra nyheter: CC1101:s variabla paketläge
(`PKTCTRL0.LENGTH_CONFIG=1`) fungerar exakt som vårt protokoll redan är
byggt — längdbytet CC1101 letar efter är precis `raw[8]`, och
hårdvaru-CRC:n (`CRC_EN=1`) är bit-för-bit samma algoritm som `crc16_cms()`.
Det betyder att chippet kan verifiera paket helt själv (RX FIFO ger bara
giltiga paket, flaggan `CRC_OK` i statusbyten), utan att vi behöver
implementera CRC i C++ alls.

Se `registers.h` för den fullständiga registertabellen med motivering per
register, och `cc1101_uponor.cpp` för hur den skrivs vid uppstart.

## 3. Bring-up-plan

1. Koppla enligt ovan, flasha `example-bringup.yaml`.
2. Komponenten läser `PARTNUM`/`VERSION` över SPI vid start och loggar dem.
   Om det inte funkar (0x00 eller 0xFF tillbaka) är det nästan alltid
   felkopplad SPI eller fel spänning — inte RF-konfigurationen.
3. När SPI är verifierad: komponenten går till RX och loggar varje paket som
   CC1101:s hårdvara accepterar (sync hittad + CRC OK), som hex. Håll en
   mottagare (RTL-SDR-loggen) igång samtidigt och jämför — hex-bytena ska
   vara identiska med vad RTL-SDR-mottagaren redan dekodar.
4. Om inget tas emot alls: prova `MDMCFG2 = 0x03` (ren 2-FSK) istället för
   `0x13` (GFSK) — se kommentar i `registers.h`. Om paket kommer men med fel
   CRC ofta: öka `DEVIATN` något (chippet klarar drift bättre med lite mer
   marginal) eller dubbelkolla antennen/avståndet till en termostat.
5. Nästa steg efter att rå byte-mottagning är verifierad (separat uppgift):
   portera TLV-parsningen (`protocol.py`) till C++ och publicera
   temperatur/setpoint per rum som ESPHome-sensorer, med samma
   rumsnamn-mappning som `rooms.py`.
