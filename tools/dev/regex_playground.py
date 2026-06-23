"""Small playground for testing description-cleaning regexes locally."""

import re

texts = [
    "Trenitalia Lefrecce Roma | Effettuato Il 31/01/2026 Alle Ore 1250 Mediante La Carta 5341 Xxxx Xxxx Xx40 Presso Trenitalia Lefrecce Roma",
    "Bonifico istantaneo da voi disposto a favore di Daniele Magri | 3526013184395777 02INTER20260131HSRT1520171636 033852438405200481601199999IT Bonifico da Voi disposto a favore di Daniele Magri Hotel firenze",
    "Apple.com/bill Cork | EFFETTUATO IL 25/01/2026 ALLE ORE 1517 MEDIANTE LA CARTA 5341 XXXX XXXX XX40 PRESSO APPLE.COM/BILL CORK",
    "Pagamento Effettuato Su Pos Estero | Effettuato Il 18/01/2026 Alle Ore 1338 (ora Autorizzazione) Mediante La Carta 5341 Xxxx Xxxx Xx40 Presso Porkbun.com Sherwood (ctv. Di 1081 Usd Al Cambio Di 0863334)",
    "Versamento Contanti Su Sportello Automatico | Effettuato presso ABI 01025 ATM 6217 il 08.01.2026 alle ore 1236 carta n. 5341********740",
    "Mcdonald's Lecce Via .. Via | MCDONALD'S LECCE VIA .. VIA06/01 2110 Carta n.5341 XXXX XXXX XX40ABI 02008 COD.3375354/000095"
]

def clean(text):
    # 1. Strip standard POS boilerplate ending in "Presso " or "presso "
    text = re.sub(r"(?i)\beffettuato(?:\s+il\s+\d{2}[/\.]\d{2}[/\.]\d{4})?\s+(?:alle\s+ore\s+\d{4})?.*?\bpresso\s+", "", text)
    # 2. Strip "Bonifico (istantaneo) da voi disposto a favore di "
    text = re.sub(r"(?i)bonifico\s+(?:istantaneo\s+)?da\s+(?:voi\s+)?disposto\s+a\s+favore\s+di\s+", "Bonifico a: ", text)
    # 3. Strip long alphanumeric codes / IDs (like 02INTER2026...)
    text = re.sub(r"\b[A-Za-z0-9]{15,}\b", "", text)
    # 4. Strip boilerplate ATM
    text = re.sub(r"(?i)\beffettuato\s+presso\s+ABI\s+\d+.*?\bcarta\s+n\.?\s+\d+\*+\d+", "", text)
    # Clean up spaces
    text = re.sub(r"\s+", " ", text).strip()
    
    # Optional: if the part before | is basically contained in the part after |, we can just keep the part after |
    parts = [p.strip() for p in text.split("|")]
    if len(parts) == 2:
        p1, p2 = parts
        if p1.lower() in p2.lower() and len(p1) > 5:
            text = p2
        elif p2.lower() in p1.lower() and len(p2) > 5:
            text = p1
            
    return text


def main() -> None:
    for text in texts:
        print(clean(text))


if __name__ == "__main__":
    main()
