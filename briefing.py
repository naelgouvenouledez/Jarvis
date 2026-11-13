#!/usr/bin/env python3
"""Briefing du matin de Jarvis : météo + actus + tes rappels, envoyé par e-mail, notification ou SMS.
Tout se règle dans briefing.json. Les secrets (mots de passe, clés) viennent des « Secrets » GitHub."""
import json, os, smtplib, ssl, sys, urllib.parse, urllib.request, xml.etree.ElementTree as ET
from datetime import datetime
from email.message import EmailMessage
from zoneinfo import ZoneInfo

HERE = os.path.dirname(os.path.abspath(__file__))
CFG = json.load(open(os.path.join(HERE, "briefing.json"), encoding="utf-8"))
DRY = os.environ.get("DRY_RUN") == "1"
UA = {"User-Agent": "jarvis-briefing/1.0"}
JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
MOIS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre", "novembre", "décembre"]


def http(url, headers=None, data=None, timeout=20):
    req = urllib.request.Request(url, data=data, headers={**UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def get_json(url, headers=None, data=None):
    return json.loads(http(url, headers, data))


def ciel(c):
    if c == 0: return "ciel dégagé"
    if c <= 2: return "ciel plutôt dégagé"
    if c == 3: return "ciel couvert"
    if c <= 48: return "brouillard"
    if c <= 57: return "bruine"
    if c <= 67: return "pluie"
    if c <= 77: return "neige"
    if c <= 82: return "averses"
    if c <= 86: return "averses de neige"
    return "orage"


def meteo(ville):
    g = get_json("https://geocoding-api.open-meteo.com/v1/search?count=1&language=fr&name=" + urllib.parse.quote(ville))
    p = g["results"][0]
    w = get_json("https://api.open-meteo.com/v1/forecast?latitude=%s&longitude=%s"
                 "&current=temperature_2m,weather_code&daily=temperature_2m_max,temperature_2m_min,precipitation_probability_max"
                 "&timezone=auto&forecast_days=1" % (p["latitude"], p["longitude"]))
    c, d = w["current"], w["daily"]
    return "%s : %d°C, %s. Aujourd'hui %d° à %d°, risque de pluie %d %%." % (
        p["name"], round(c["temperature_2m"]), ciel(c["weather_code"]),
        round(d["temperature_2m_min"][0]), round(d["temperature_2m_max"][0]), d["precipitation_probability_max"][0])


def titres(flux, n):
    out = []
    for url in flux:
        try:
            root = ET.fromstring(http(url))
            out += [t for t in ((i.findtext("title") or "").strip() for i in root.iter("item")) if t][:n]
        except Exception as e:
            print("Flux ignoré :", url, "->", e)
    return out


def faits():
    now = datetime.now(ZoneInfo("Europe/Paris"))
    lignes = ["Date : %s %d %s" % (JOURS[now.weekday()], now.day, MOIS[now.month - 1])]
    if CFG.get("meteo", True):
        try: lignes.append("Météo : " + meteo(CFG["ville"]))
        except Exception as e: print("Météo ignorée ->", e)
    if CFG.get("rappels"):
        lignes.append("À faire aujourd'hui : " + " ; ".join(CFG["rappels"]))
    if CFG.get("actus", True):
        t = titres(CFG.get("flux_rss", []), CFG.get("titres_par_flux", 3))
        if t: lignes.append("Actualités : " + " | ".join(t))
    return lignes


def modele_simple(lignes):
    return "Bonjour %s.\n\n%s" % (CFG["surnom"], "\n".join(lignes))


def rediger(lignes):
    key = os.environ.get("GROQ_API_KEY")
    if not key:
        return None
    body = {"model": CFG.get("modele_ia", "openai/gpt-oss-20b"), "messages": [
        {"role": "system", "content": CFG["style"] + " Appelle l'utilisateur « %s »." % CFG["surnom"]},
        {"role": "user", "content": "Rédige le briefing du matin à partir de ces informations, sans rien inventer, "
                                    "en moins de %d caractères :\n%s" % (CFG.get("longueur_max", 900), "\n".join(lignes))}]}
    d = get_json("https://api.groq.com/openai/v1/chat/completions",
                 {"Authorization": "Bearer " + key, "Content-Type": "application/json"}, json.dumps(body).encode())
    return d["choices"][0]["message"]["content"].strip()


def envoyer_email(sujet, texte):
    u, pw, to = (os.environ.get(k) for k in ("EMAIL_USER", "EMAIL_PASS", "EMAIL_TO"))
    if not (u and pw and to): return False
    msg = EmailMessage(); msg["Subject"] = sujet; msg["From"] = u; msg["To"] = to; msg.set_content(texte)
    with smtplib.SMTP_SSL(os.environ.get("SMTP_HOST", "smtp.gmail.com"), int(os.environ.get("SMTP_PORT", "465")),
                          context=ssl.create_default_context()) as s:
        s.login(u, pw); s.send_message(msg)
    return True


def envoyer_push(sujet, texte):
    topic = os.environ.get("NTFY_TOPIC")
    if not topic: return False
    http("https://ntfy.sh/" + urllib.parse.quote(topic), {"Title": "Briefing du matin"}, texte.encode("utf-8"))
    return True


def envoyer_sms(texte):
    user, pw = os.environ.get("FREE_USER"), os.environ.get("FREE_PASS")   # SMS gratuit, abonnés Free Mobile
    if not (user and pw): return False
    http("https://smsapi.free-mobile.fr/sendmsg?user=%s&pass=%s&msg=%s" % (
        urllib.parse.quote(user), urllib.parse.quote(pw), urllib.parse.quote(texte[:600])))
    return True


def main():
    lignes = faits()
    try: texte = rediger(lignes)
    except Exception as e: print("IA ignorée ->", e); texte = None
    texte = texte or modele_simple(lignes)
    sujet = "Briefing du matin"
    if DRY:
        print(texte); return 0
    echecs = 0
    for canal, actif in CFG.get("canaux", {}).items():
        if not actif: continue
        try:
            ok = {"email": lambda: envoyer_email(sujet, texte), "push": lambda: envoyer_push(sujet, texte),
                  "sms": lambda: envoyer_sms(texte)}[canal]()
        except Exception as e:
            print("Échec", canal, "->", e); ok = False
        print(canal, "envoyé" if ok else "NON envoyé (secrets manquants ou erreur)")
        echecs += 0 if ok else 1
    return 1 if echecs else 0


if __name__ == "__main__":
    sys.exit(main())
