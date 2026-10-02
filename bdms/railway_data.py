"""Master data for the Add Asset form on the Worker dashboard.

Structure:  Division -> sections / stations / junctions

The Add Asset form is a chain of drop-downs: pick a Division first and the
Section, From/To Station and From/To Junction lists fill with only that
division's entries.

>>> This is SAMPLE data for Southern Railway. Check it against your official
>>> records and edit it freely: add, rename or remove lines below and restart
>>> the server. No database migration is needed.
"""

RAILWAY_DATA = {
    "Chennai": {
        "sections": [
            "Chennai Central - Arakkonam",
            "Arakkonam - Katpadi",
            "Chennai Central - Gudur",
            "Chennai Beach - Chengalpattu",
        ],
        "stations": [
            "Chennai Central", "Chennai Egmore", "Chennai Beach", "Basin Bridge Jn",
            "Perambur", "Avadi", "Tiruvallur", "Arakkonam Jn", "Katpadi Jn",
            "Gudur Jn", "Tambaram", "Chengalpattu Jn",
        ],
        "junctions": [
            "Basin Bridge Jn", "Arakkonam Jn", "Katpadi Jn", "Gudur Jn", "Chengalpattu Jn",
        ],
    },
    "Salem": {
        "sections": [
            "Jolarpettai - Salem",
            "Salem - Erode",
            "Erode - Coimbatore",
            "Coimbatore - Podanur",
            "Coimbatore - Mettupalayam",
        ],
        "stations": [
            "Jolarpettai Jn", "Tirupattur", "Dharmapuri", "Omalur", "Salem Jn",
            "Erode Jn", "Tiruppur", "Coimbatore Jn", "Podanur Jn", "Mettupalayam",
        ],
        "junctions": [
            "Jolarpettai Jn", "Salem Jn", "Erode Jn", "Coimbatore Jn", "Podanur Jn",
        ],
    },
    "Tiruchchirappalli": {
        "sections": [
            "Villupuram - Vriddhachalam",
            "Vriddhachalam - Tiruchchirappalli",
            "Tiruchchirappalli - Thanjavur",
            "Thanjavur - Mayiladuthurai",
            "Tiruchchirappalli - Karaikudi",
        ],
        "stations": [
            "Villupuram Jn", "Vriddhachalam Jn", "Ariyalur", "Tiruchchirappalli Jn",
            "Thanjavur Jn", "Kumbakonam", "Mayiladuthurai Jn", "Karur Jn",
            "Pudukkottai", "Karaikudi Jn",
        ],
        "junctions": [
            "Villupuram Jn", "Vriddhachalam Jn", "Tiruchchirappalli Jn",
            "Thanjavur Jn", "Mayiladuthurai Jn", "Karur Jn", "Karaikudi Jn",
        ],
    },
    "Madurai": {
        "sections": [
            "Dindigul - Madurai",
            "Madurai - Virudhunagar",
            "Virudhunagar - Tirunelveli",
            "Madurai - Manamadurai",
            "Manamadurai - Rameswaram",
            "Maniyachchi - Tuticorin",
        ],
        "stations": [
            "Dindigul Jn", "Madurai Jn", "Virudhunagar Jn", "Kovilpatti",
            "Tirunelveli Jn", "Manamadurai Jn", "Rameswaram", "Maniyachchi Jn", "Tuticorin",
        ],
        "junctions": [
            "Dindigul Jn", "Madurai Jn", "Virudhunagar Jn", "Manamadurai Jn",
            "Maniyachchi Jn", "Tirunelveli Jn",
        ],
    },
    "Palakkad": {
        "sections": [
            "Palakkad - Shoranur",
            "Shoranur - Kozhikode",
            "Kozhikode - Kannur",
            "Kannur - Mangaluru",
            "Shoranur - Nilambur Road",
            "Palakkad - Pollachi",
        ],
        "stations": [
            "Palakkad Jn", "Pollachi Jn", "Shoranur Jn", "Kozhikode", "Kannur",
            "Mangaluru Jn", "Nilambur Road",
        ],
        "junctions": [
            "Palakkad Jn", "Pollachi Jn", "Shoranur Jn", "Mangaluru Jn",
        ],
    },
    "Thiruvananthapuram": {
        "sections": [
            "Thiruvananthapuram - Kollam",
            "Kollam - Kayamkulam",
            "Kayamkulam - Ernakulam",
            "Kottayam - Ernakulam",
            "Ernakulam - Thrissur",
            "Thiruvananthapuram - Nagercoil",
            "Nagercoil - Kanyakumari",
        ],
        "stations": [
            "Thiruvananthapuram Central", "Kochuveli", "Kollam Jn", "Kayamkulam Jn",
            "Alappuzha", "Chengannur", "Kottayam", "Ernakulam Jn", "Thrissur",
            "Nagercoil Jn", "Kanyakumari",
        ],
        "junctions": [
            "Kollam Jn", "Kayamkulam Jn", "Ernakulam Jn", "Nagercoil Jn",
        ],
    },
}