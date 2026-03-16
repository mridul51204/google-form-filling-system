#!/usr/bin/env python3
# /runner.py

"""
Google Forms Runner (Playwright, sync) with wizard V2 config compatibility.

Hardening + performance:
- Per-step caching: section signature + visible blocks computed once per loop iteration and reused.
- Probe timeouts bounded (<=250ms) and guarded by count()>0.
- Faster nav polling after Next/Submit (<=6s, 200ms sleep): cheap nav-labels/body-hash first, then full signature once.
- Robust listbox dropdown primitive: click listbox, wait aria-expanded=true, click option by data-value, verify aria-selected moved, Esc close.
- Fill-before-next ALWAYS and repair-once if navigation doesn't happen.
- Diagnostics only on failure.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import json
import random
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

RespondentState = Dict[str, Any]


# -----------------------------
# CLI helpers
# -----------------------------


def str_to_bool(v: str) -> bool:
    s = v.strip().lower()
    if s in {"1", "true", "t", "yes", "y", "on"}:
        return True
    if s in {"0", "false", "f", "no", "n", "off"}:
        return False
    raise argparse.ArgumentTypeError(f"Invalid boolean: {v!r}. Use true/false.")


# -----------------------------
# Normalization helpers
# -----------------------------


def normalize_button_label(text: str) -> str:
    return " ".join((text or "").split()).strip()


def normalize_label_to_key(label: str) -> str:
    s = (label or "").strip().lower()
    s = re.sub(r"[*]+$", "", s).strip()
    s = re.sub(r"[^a-z0-9]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s


def normalize_question_identity(label: str) -> str:
    base = normalize_label_to_key(label)
    return base or "question"


def is_other_option(text: str) -> bool:
    low = (text or "").strip().lower()
    return low == "other" or low == "other:" or low.startswith("other")


def get_fill_priority(ident: str) -> int:
    low = ident.lower()
    if "gender" in low or "sex" in low:
        return 0
    if "name" in low:
        return 1
    if "email" in low:
        return 2
    return 10


# -----------------------------
# RNG + Persona
# -----------------------------


MALE_FIRST_NAMES = [
    "Aarav",
    "Aaditya",
    "Aakash",
    "Aamir",
    "Aayan",
    "Abhay",
    "Abhinav",
    "Adarsh",
    "Aditya",
    "Advait",
    "Ahaan",
    "Ajay",
    "Akash",
    "Akhil",
    "Akshay",
    "Alok",
    "Aman",
    "Amar",
    "Amit",
    "Amogh",
    "Amrit",
    "Anand",
    "Aniket",
    "Anirudh",
    "Anish",
    "Ankit",
    "Ankur",
    "Anmol",
    "Ansh",
    "Anuj",
    "Anurag",
    "Arjun",
    "Arnav",
    "Arpit",
    "Aryan",
    "Ashish",
    "Ashwin",
    "Atul",
    "Avinash",
    "Ayush",
    "Bala",
    "Balaji",
    "Bhanu",
    "Bharat",
    "Bhaskar",
    "Bhuvan",
    "Bikram",
    "Bimal",
    "Binay",
    "Brijesh",
    "Chetan",
    "Chirag",
    "Darshan",
    "Deepak",
    "Devesh",
    "Devraj",
    "Dhruv",
    "Dinesh",
    "Divyansh",
    "Eshan",
    "Faizan",
    "Farhan",
    "Gagan",
    "Gaurav",
    "Girish",
    "Govind",
    "Gopal",
    "Gourav",
    "Harish",
    "Harsh",
    "Himanshu",
    "Hriday",
    "Ibrahim",
    "Ishaan",
    "Ishan",
    "Jai",
    "Jaidev",
    "Jatin",
    "Jeevan",
    "Jitendra",
    "Kabilan",
    "Kabir",
    "Kailash",
    "Kamal",
    "Kannan",
    "Karan",
    "Karthik",
    "Keshav",
    "Ketan",
    "Kiran",
    "Krishna",
    "Kunal",
    "Lakshya",
    "Lalit",
    "Lokesh",
    "Madan",
    "Madhav",
    "Mahesh",
    "Manish",
    "Manoj",
    "Mayank",
    "Mihir",
    "Mohammed",
    "Mohit",
    "Mukesh",
    "Nakul",
    "Nandan",
    "Naveen",
    "Nikhil",
    "Nilesh",
    "Niranjan",
    "Nitin",
    "Omkar",
    "Pankaj",
    "Parth",
    "Prabhakar",
    "Pradeep",
    "Prakash",
    "Pranav",
    "Prashant",
    "Prateek",
    "Prem",
    "Puneet",
    "Raghav",
    "Rahul",
    "Raj",
    "Raja",
    "Rajesh",
    "Rakesh",
    "Raman",
    "Ramesh",
    "Ranjit",
    "Ravi",
    "Rishabh",
    "Ritesh",
    "Rohit",
    "Rohan",
    "Roshan",
    "Sachin",
    "Sagar",
    "Sahil",
    "Sai",
    "Sanjay",
    "Sandeep",
    "Sandesh",
    "Sathish",
    "Satish",
    "Saurabh",
    "Shaurya",
    "Shivam",
    "Shyam",
    "Siddharth",
    "Siddhesh",
    "Soham",
    "Sourav",
    "Srijan",
    "Subhash",
    "Sudhir",
    "Suhas",
    "Sujit",
    "Sumit",
    "Sunil",
    "Suraj",
    "Suresh",
    "Swapnil",
    "Tarun",
    "Tejas",
    "Uday",
    "Ujjwal",
    "Varun",
    "Vedant",
    "Vijay",
    "Vikram",
    "Vimal",
    "Vinay",
    "Vineet",
    "Vinit",
    "Vishal",
    "Vivek",
    "Yash",
    "Yogesh",
    "Abhishek",
    "Adil",
    "Ajeet",
    "Ajith",
    "Akilan",
    "Akhilesh",
    "Amjad",
    "Anandhu",
    "Anup",
    "Anupam",
    "Arun",
    "Arvind",
    "Aseem",
    "Asif",
    "Atharv",
    "Avaneesh",
    "Bharani",
    "Bhavesh",
    "Chandan",
    "Chandrahas",
    "Chinmay",
    "Dhanush",
    "Dharam",
    "Dheeraj",
    "Durgesh",
    "Eklavya",
    "Gautam",
    "Gokul",
    "Gurpreet",
    "Hardik",
    "Hemant",
    "Inder",
    "Jayant",
    "Jignesh",
    "Jishnu",
    "Kailas",
    "Kishan",
    "Krish",
    "Kushal",
    "Mandeep",
    "Manpreet",
    "Naveendra",
    "Neeraj",
    "Nischal",
    "Nitesh",
    "Piyush",
    "Prabhjot",
    "Pranesh",
    "Pratik",
    "Rahim",
    "Rajeev",
    "Rajiv",
    "Rajkumar",
    "Ravindra",
    "Saket",
    "Sameer",
    "Santosh",
    "Sarvesh",
    "Shailesh",
    "Shankar",
    "Sharan",
    "Shashi",
    "Shivendra",
    "Shubham",
    "Sudarshan",
    "Sumeet",
    "Sunit",
    "Sushil",
    "Tushar",
    "Venkatesh",
    "Vikas",
    "Viraj",
    "Vishwanath",
    "Yuvraj",
    "Zaid",
]

FEMALE_FIRST_NAMES = [
    "Aadhya",
    "Aakriti",
    "Aanya",
    "Aarohi",
    "Aashi",
    "Aashna",
    "Aditi",
    "Adwita",
    "Ahana",
    "Aishwarya",
    "Akanksha",
    "Akshara",
    "Alisha",
    "Alka",
    "Amala",
    "Amisha",
    "Amrita",
    "Ananya",
    "Anika",
    "Anita",
    "Anjali",
    "Ankita",
    "Annapurna",
    "Anusha",
    "Aparna",
    "Arpita",
    "Arya",
    "Ashima",
    "Avani",
    "Bhavya",
    "Bhoomi",
    "Bina",
    "Chaitali",
    "Chandana",
    "Charu",
    "Chhavi",
    "Daisy",
    "Deepa",
    "Deepika",
    "Deepti",
    "Devika",
    "Dhanya",
    "Divya",
    "Drishti",
    "Eesha",
    "Eshita",
    "Farah",
    "Gargi",
    "Gayatri",
    "Geeta",
    "Gitanjali",
    "Harini",
    "Heena",
    "Hema",
    "Hina",
    "Ila",
    "Isha",
    "Ishani",
    "Jahnavi",
    "Jaya",
    "Jayashree",
    "Jyoti",
    "Kajal",
    "Kalpana",
    "Kamini",
    "Kanchan",
    "Kavya",
    "Keerthi",
    "Kiran",
    "Kritika",
    "Lakshmi",
    "Lalita",
    "Leela",
    "Madhuri",
    "Mahima",
    "Malini",
    "Manasa",
    "Manisha",
    "Meena",
    "Meera",
    "Minal",
    "Mira",
    "Mitali",
    "Mohini",
    "Monika",
    "Mridula",
    "Naina",
    "Namrata",
    "Nandini",
    "Narmada",
    "Navya",
    "Neelam",
    "Neha",
    "Nidhi",
    "Niharika",
    "Nikita",
    "Nisha",
    "Nithya",
    "Ojasvi",
    "Pallavi",
    "Parul",
    "Pooja",
    "Pragya",
    "Pranati",
    "Pratibha",
    "Preeti",
    "Priya",
    "Purnima",
    "Rachana",
    "Radha",
    "Rajni",
    "Rani",
    "Rashmi",
    "Raveena",
    "Rekha",
    "Rhea",
    "Rina",
    "Ritika",
    "Riya",
    "Roopa",
    "Roshni",
    "Sakshi",
    "Sangeeta",
    "Sanjana",
    "Sapna",
    "Saraswati",
    "Sasha",
    "Seema",
    "Shalini",
    "Sharmila",
    "Shilpa",
    "Shivani",
    "Shreya",
    "Shruti",
    "Simran",
    "Smita",
    "Sneha",
    "Sonia",
    "Sreelakshmi",
    "Srishti",
    "Sunita",
    "Supriya",
    "Sushma",
    "Swati",
    "Tanvi",
    "Tara",
    "Trisha",
    "Uma",
    "Upasana",
    "Vaidehi",
    "Vandana",
    "Varsha",
    "Vasudha",
    "Veda",
    "Vidya",
    "Vineeta",
    "Yamini",
    "Aaradhya",
    "Aastha",
    "Aayushi",
    "Akhila",
    "Alpana",
    "Anamika",
    "Anushka",
    "Aradhana",
    "Archana",
    "Bhavana",
    "Bhakti",
    "Chitra",
    "Damini",
    "Dhanashree",
    "Esha",
    "Falguni",
    "Gauri",
    "Ishita",
    "Jasleen",
    "Kiranmayi",
    "Kusum",
    "Lata",
    "Madhu",
    "Mansi",
    "Mayuri",
    "Meghna",
    "Nandita",
    "Padma",
    "Pavithra",
    "Prerna",
    "Radhika",
    "Ragini",
    "Rajalakshmi",
    "Rajeshwari",
    "Ritu",
    "Rupali",
    "Sahana",
    "Shanaya",
    "Shanti",
    "Shubhangi",
    "Suhani",
    "Suma",
    "Sushmita",
    "Tanya",
    "Urvashi",
    "Vani",
    "Veena",
    "Vrinda",
    "Yashika",
    "Zara",
]

NEUTRAL_FIRST_NAMES = [
    "Aman",
    "Ari",
    "Sam",
    "Dev",
    "Noor",
    "Ray",
]

LAST_NAMES = [
    "Acharya",
    "Agarwal",
    "Ahluwalia",
    "Ahuja",
    "Anand",
    "Arora",
    "Babu",
    "Bajaj",
    "Bakshi",
    "Balakrishnan",
    "Banerjee",
    "Bansal",
    "Basu",
    "Batra",
    "Bedi",
    "Bhagat",
    "Bhandari",
    "Bhardwaj",
    "Bhattacharya",
    "Bhatia",
    "Bhosale",
    "Biswas",
    "Bose",
    "Chakraborty",
    "Chandra",
    "Chatterjee",
    "Chaudhary",
    "Chauhan",
    "Chettiar",
    "Chopra",
    "Das",
    "Datta",
    "Desai",
    "Deshpande",
    "Devi",
    "Dey",
    "Dhar",
    "Dubey",
    "Dutta",
    "Gandhi",
    "Ganguly",
    "Garg",
    "Ghosh",
    "Gill",
    "Goel",
    "Gopalakrishnan",
    "Goswami",
    "Guha",
    "Gupta",
    "Iyer",
    "Jadhav",
    "Jain",
    "Jha",
    "Joshi",
    "Kadam",
    "Kapoor",
    "Kaur",
    "Khan",
    "Khanna",
    "Khatri",
    "Kishore",
    "Kohli",
    "Kulkarni",
    "Kumar",
    "Lal",
    "Mahajan",
    "Malhotra",
    "Mandal",
    "Mehta",
    "Menon",
    "Mishra",
    "Mittal",
    "Mukherjee",
    "Nair",
    "Narayan",
    "Nath",
    "Naidu",
    "Nanda",
    "Panda",
    "Pandey",
    "Parikh",
    "Patel",
    "Pathak",
    "Pillai",
    "Prasad",
    "Raghavan",
    "Rai",
    "Rajput",
    "Rana",
    "Rao",
    "Rastogi",
    "Reddy",
    "Roy",
    "Sahu",
    "Saksena",
    "Saxena",
    "Sen",
    "Shah",
    "Sharma",
    "Shetty",
    "Shukla",
    "Singh",
    "Sinha",
    "Sridhar",
    "Srivastava",
    "Subramanian",
    "Sundaram",
    "Swamy",
    "Thakur",
    "Trivedi",
    "Varma",
    "Verma",
    "Yadav",
    "Bahl",
    "Bairwa",
    "Balan",
    "Bamrah",
    "Bari",
    "Bashir",
    "Bera",
    "Bhowmick",
    "Birla",
    "Bisht",
    "Bora",
    "Chakrabarti",
    "Chhabra",
    "Chikermane",
    "Dabholkar",
    "Dalal",
    "Dhawan",
    "Dholakia",
    "Dixit",
    "Gade",
    "Gairola",
    "Gaikwad",
    "Gera",
    "Gore",
    "Grover",
    "Hegde",
    "Jaiswal",
    "Jamwal",
    "Juneja",
    "Kale",
    "Kalia",
    "Kamble",
    "Kanth",
    "Kankaria",
    "Kapur",
    "Kar",
    "Karandikar",
    "Karim",
    "Karkhanis",
    "Kaul",
    "Kesavan",
    "Khurana",
    "Kothari",
    "Kukreja",
    "Lamba",
    "Lohia",
    "Madane",
    "Maira",
    "Majumdar",
    "Mankad",
    "Marathe",
    "Mathur",
    "Mavani",
    "Memon",
    "Mundra",
    "Nadkarni",
    "Nagpal",
    "Ojha",
    "Pahuja",
    "Pal",
    "Panicker",
    "Parekh",
    "Paswan",
    "Pawar",
    "Poonia",
    "Punj",
    "Ramakrishnan",
    "Rangnekar",
    "Raval",
    "Rawat",
    "Sarin",
    "Seth",
    "Sengupta",
    "Sethi",
    "Sodhi",
    "Soman",
    "Soni",
    "Sood",
    "Tandon",
    "Tiwari",
    "Upadhyay",
    "Venkataraman",
    "Vyas",
    "Wadhwa",
    "Amin",
    "Ansari",
    "Antony",
    "Awasthi",
    "Badal",
    "Bagchi",
    "Bai",
    "Bairagi",
    "Balmiki",
    "Baruah",
    "Bavishi",
    "Behl",
    "Bhandarkar",
    "Bhowmik",
    "Brahmbhatt",
    "Chadha",
    "Chand",
    "Chandran",
    "Chary",
    "Chaudhari",
    "Chaudhury",
    "Chibber",
    "Choudhary",
    "Damodaran",
    "Deol",
    "Dharan",
    "Dharwadkar",
    "Dhingra",
    "Gaba",
    "Gadkari",
    "Gandotra",
    "Gosain",
    "Gowda",
    "Gulati",
    "Haldar",
    "Hussain",
    "Imam",
    "Jagannath",
    "Jameel",
    "Kakkar",
    "Kankaria",
    "Kapur",
    "Kar",
    "Karandikar",
    "Karim",
    "Karkhanis",
    "Kaul",
    "Kesavan",
    "Khurana",
    "Kothari",
    "Krishnan",
    "Kukreja",
    "Lamba",
    "Lohia",
    "Madane",
    "Maira",
    "Majumdar",
    "Mankad",
    "Marathe",
    "Mathur",
    "Mavani",
    "Memon",
    "Mundra",
    "Nadkarni",
    "Nagpal",
    "Ojha",
    "Pahuja",
    "Pal",
    "Panicker",
    "Parekh",
    "Paswan",
    "Pawar",
    "Poonia",
    "Punj",
    "Ramakrishnan",
    "Rangnekar",
    "Raval",
    "Rawat",
    "Sarin",
    "Seth",
    "Sengupta",
    "Sethi",
    "Sodhi",
    "Soman",
    "Soni",
    "Sood",
    "Tandon",
    "Tiwari",
    "Upadhyay",
    "Vadlamani",
    "Vaid",
    "Venkatesan",
    "Vishwakarma",
    "Walia",
]

DEFAULT_CITY_STATE_POOL = [
    "Mumbai, Maharashtra",
    "Delhi, Delhi",
    "Bengaluru, Karnataka",
    "Hyderabad, Telangana",
    "Chennai, Tamil Nadu",
    "Kolkata, West Bengal",
    "Pune, Maharashtra",
]

# Optional external name pools (one per line).
_NAMES_DATA_DIR = Path(__file__).resolve().parent / "data"


def _load_names_file(path: Path) -> List[str]:
    try:
        if not path.exists():
            return []
        lines = [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines()]
        out: List[str] = []
        seen = set()
        for ln in lines:
            if not ln or ln.startswith("#"):
                continue
            name = " ".join(ln.split())
            if not name:
                continue
            name = re.sub(r"[^A-Za-z\s'-]", "", name).strip()
            if not name:
                continue
            key = name.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(name)
        return out
    except Exception:
        return []


def _name_pool_from_tuning_or_file(
    tuning: Dict[str, Any], tuning_key: str, file_name: str, fallback: List[str]
) -> List[str]:
    pool = tuning.get(tuning_key)
    if isinstance(pool, list) and pool:
        return list(dict.fromkeys([str(x).strip() for x in pool if str(x).strip()]))
    loaded = _load_names_file(_NAMES_DATA_DIR / file_name)
    return loaded if loaded else fallback


def _rng(state: RespondentState) -> random.Random:
    r = state.get("_rng")
    if isinstance(r, random.Random):
        return r
    seed = int(state.get("rng_seed") or 0) or random.SystemRandom().randint(1, 2**31 - 1)
    state["rng_seed"] = seed
    state["_rng"] = random.Random(seed)
    return state["_rng"]


def _normalize_gender(value: Any) -> str:
    s = str(value or "").strip().lower()
    if s in {"male", "m", "man", "boy"}:
        return "male"
    if s in {"female", "f", "woman", "girl"}:
        return "female"
    return "other"


def _gender_ui_value(canonical: str) -> str:
    c = (canonical or "").strip().lower()
    if c == "male":
        return "Male"
    if c == "female":
        return "Female"
    return "Prefer not to say"


def set_gender_ui(state: RespondentState, ui_value: str) -> None:
    state["gender_ui"] = ui_value
    state["gender_canonical"] = _normalize_gender(ui_value)
    state["gender"] = state["gender_canonical"]
    assert _normalize_gender(state["gender_ui"]) == state["gender_canonical"]


def _digits(rng: random.Random, n: int) -> str:
    return "".join(rng.choice("0123456789") for _ in range(max(0, int(n))))


def _make_email_from_name(
    rng: random.Random,
    first: str,
    last: str,
    domain: str,
    suffix_digits: int = 3,
    local_part_pattern: str = "first.last",
) -> str:
    first_low = first.lower()
    last_low = last.lower()
    if local_part_pattern == "first.last":
        local = f"{first_low}.{last_low}"
    elif local_part_pattern == "first_last":
        local = f"{first_low}_{last_low}"
    else:
        local = f"{first_low}{last_low}"
    local = re.sub(r"[^a-zA-Z0-9]+", "", local) or "testuser"
    return f"{local}{_digits(rng, suffix_digits)}@{domain}"


def _make_phone_in(rng: random.Random) -> str:
    first = rng.choice("6789")
    rest = _digits(rng, 9)
    return first + rest


def _ensure_persona_state(tuning: Dict[str, Any], state: RespondentState) -> None:
    rng = _rng(state)

    if state.get("gender_canonical") is None:
        r = rng.random()
        if r < 0.40:
            gender = "Male"
        elif r < 0.90:
            gender = "Female"
        else:
            gender = "Prefer not to say"

        set_gender_ui(state, gender)
        assert _normalize_gender(state["gender_ui"]) == state["gender_canonical"]

        if not state.get("_gender_pick_logged"):
            print(f"GENDER_PICK: r={r:.6f} gender={gender}")
            print(f"GENDER_STATE: canonical={state['gender_canonical']} ui={state['gender_ui']}")
            state["_gender_pick_logged"] = True

    if not state.get("name_first"):
        male_pool = _name_pool_from_tuning_or_file(tuning, "male_first_names", "names_male.txt", MALE_FIRST_NAMES)
        female_pool = _name_pool_from_tuning_or_file(tuning, "female_first_names", "names_female.txt", FEMALE_FIRST_NAMES)
        neutral_pool = tuning.get("neutral_first_names") or NEUTRAL_FIRST_NAMES

        gender = state.get("gender_canonical")
        if gender == "male":
            pool = male_pool
        elif gender == "female":
            pool = female_pool
        else:
            pool = male_pool if rng.random() < 0.5 else female_pool

        state["name_first"] = rng.choice(pool)
        state["_persona_auto_first"] = True

    if not state.get("name_last"):
        last_pool = _name_pool_from_tuning_or_file(tuning, "last_names", "last_names.txt", LAST_NAMES)
        state["name_last"] = rng.choice(last_pool)
        state["_persona_auto_last"] = True

    if not state.get("name_full"):
        state["name_full"] = f"{state['name_first']} {state['name_last']}".strip()
        state["_persona_auto_full"] = True

    if not state.get("city_state"):
        pool = tuning.get("city_state_pool") or DEFAULT_CITY_STATE_POOL
        state["city_state"] = str(rng.choice(pool)) if isinstance(pool, list) and pool else "Mumbai, Maharashtra"

    if not state.get("phone"):
        override = tuning.get("phone")
        if isinstance(override, str) and override.strip():
            state["phone"] = override.strip()
        else:
            state["phone"] = _make_phone_in(rng)
            state["_persona_auto_phone"] = True

    if not state.get("email"):
        domain = str(tuning.get("email_domain") or "gmail.com")
        suffix_digits = int(tuning.get("email_suffix_digits") or 3)
        local_part_pattern = str(tuning.get("email_local_part_pattern") or "first.last")
        state["email"] = _make_email_from_name(
            rng=rng,
            first=str(state["name_first"]),
            last=str(state["name_last"]),
            domain=domain,
            suffix_digits=suffix_digits,
            local_part_pattern=local_part_pattern,
        )
        state["_persona_auto_email"] = True

    state.setdefault("first_name", state["name_first"])
    state.setdefault("last_name", state["name_last"])
    state.setdefault("name", state["name_full"])
    state.setdefault("gender_ui", _gender_ui_value(state["gender_canonical"]))
    state.setdefault("_last_answers", {})


def _apply_gender_override(tuning: Dict[str, Any], state: RespondentState, value: Any) -> None:
    if not value:
        return
    old_gender = state.get("gender_canonical")
    set_gender_ui(state, str(value))

    if state.get("_persona_auto_first") and old_gender != state["gender_canonical"]:
        rng = _rng(state)
        male_pool = _name_pool_from_tuning_or_file(tuning, "male_first_names", "names_male.txt", MALE_FIRST_NAMES)
        female_pool = _name_pool_from_tuning_or_file(tuning, "female_first_names", "names_female.txt", FEMALE_FIRST_NAMES)
        neutral_pool = tuning.get("neutral_first_names") or NEUTRAL_FIRST_NAMES

        new_gender = state["gender_canonical"]
        if new_gender == "male":
            pool = male_pool
        elif new_gender == "female":
            pool = female_pool
        else:
            pool = male_pool if rng.random() < 0.5 else female_pool

        state["name_first"] = rng.choice(pool)
        state["first_name"] = state["name_first"]

        if state.get("_persona_auto_full"):
            state["name_full"] = f"{state['name_first']} {state['name_last']}".strip()
            state["name"] = state["name_full"]

        if state.get("_persona_auto_email"):
            domain = str(tuning.get("email_domain") or "gmail.com")
            suffix_digits = int(tuning.get("email_suffix_digits") or 3)
            local_part_pattern = str(tuning.get("email_local_part_pattern") or "first.last")
            state["email"] = _make_email_from_name(
                rng=rng,
                first=str(state["name_first"]),
                last=str(state["name_last"]),
                domain=domain,
                suffix_digits=suffix_digits,
                local_part_pattern=local_part_pattern,
            )


def _is_gender_field_label(label: str) -> bool:
    k = normalize_label_to_key(label)
    return k in {"gender", "sex"} or ("gender" in (label or "").lower())


def _maybe_sync_persona_gender_from_field(
    field: Dict[str, Any], label: str, value: Any, state: RespondentState, tuning: Dict[str, Any]
) -> None:
    gen = field.get("generation") or {}
    mode = str(gen.get("mode") or "").strip().upper()
    if mode != "WEIGHTED":
        return
    if not _is_gender_field_label(label):
        return
    if isinstance(value, str) and value.strip():
        _apply_gender_override(tuning, state, value)


# -----------------------------
# Generation (wizard V2 compatibility)
# -----------------------------


_TEMPLATE_VAR_RE = re.compile(r"\{([a-zA-Z0-9_.:]+)\}")


def render_pattern(template: str, state: RespondentState, rng: random.Random) -> str:
    def repl(m: re.Match) -> str:
        key = m.group(1).strip()

        if key.startswith("rand:"):
            try:
                n = int(key.split(":", 1)[1])
            except Exception:
                n = 6
            return _digits(rng, n)

        if key.startswith("persona."):
            fld = key.split(".", 1)[1].strip().lower()

            if fld == "name_first":
                fld = "first_name"
            elif fld == "name_last":
                fld = "last_name"

            persona_map = {
                "first_name": state.get("name_first") or state.get("first_name") or "",
                "last_name": state.get("name_last") or state.get("last_name") or "",
                "name_full": state.get("name_full") or state.get("name") or "",
                "email": state.get("email") or "",
                "phone": state.get("phone") or "",
                "city_state": state.get("city_state") or "",
            }
            return str(persona_map.get(fld, "")) or m.group(0)

        if key.startswith("state."):
            fld = key.split(".", 1)[1].strip()
            val = state.get(fld)
            return str(val) if val is not None else m.group(0)

        return m.group(0)

    return _TEMPLATE_VAR_RE.sub(repl, template or "")


def _render_pattern(template: str, state: RespondentState) -> str:
    return render_pattern(template, state, _rng(state))


def _weighted_choice(rng: random.Random, items: List[Tuple[Any, float]]) -> Any:
    total = sum(max(0.0, w) for _, w in items) or 0.0
    if total <= 0:
        return items[0][0] if items else None
    r = rng.random() * total
    upto = 0.0
    for val, w in items:
        w = max(0.0, w)
        upto += w
        if upto >= r:
            return val
    return items[-1][0]


def generate_field_value(field: Dict[str, Any], state: RespondentState, tuning: Dict[str, Any]) -> Any:
    gen = field.get("generation") or {}
    mode = str(gen.get("mode") or "").strip().upper() or "INFER"
    spec = gen.get("spec") or {}
    rng = _rng(state)

    if mode in {"INFER", "SKIP"}:
        return None

    if mode in {"STATIC", "LITERAL"}:
        return spec.get("value", "")

    if mode in {"PATTERN", "TEMPLATE"}:
        _ensure_persona_state(tuning, state)
        return render_pattern(str(spec.get("template") or ""), state, rng)

    if mode == "PERSONA":
        _ensure_persona_state(tuning, state)
        k = str(gen.get("spec", {}).get("field") or "name_full").strip().lower()
        alias = {
            "name": "name_full",
            "full_name": "name_full",
            "firstname": "name_first",
            "first": "name_first",
            "lastname": "name_last",
            "last": "name_last",
            "gender_ui": "gender",
        }
        k = alias.get(k, k)
        if k == "gender":
            return state.get("gender_ui") or ""
        return state.get(k)

    if mode == "RANGE":
        mn = spec.get("min")
        mx = spec.get("max")
        is_int = bool(spec.get("integer", True))
        if mn is None or mx is None:
            return None
        if is_int:
            a, b = int(mn), int(mx)
            if b < a:
                a, b = b, a
            return str(rng.randint(a, b))
        a, b = float(mn), float(mx)
        if b < a:
            a, b = b, a
        val = a + (b - a) * rng.random()
        places = int(spec.get("decimals") or 2)
        return f"{val:.{places}f}"

    if mode == "WEIGHTED":
        choices = spec.get("choices") or []
        if not choices:
            return None
        multi = bool(spec.get("multi", False))
        items = [(c.get("value"), float(c.get("weight", 1.0))) for c in choices]

        if spec.get("grid"):
            rows = field.get("grid", {}).get("rows", [])
            out = {}
            strategy = spec.get("strategy", "per_row")
            for row in rows:
                if strategy == "per_row":
                    out[row] = _weighted_choice(rng, items)
                elif strategy == "per_row_multi":
                    min_sel = int(spec.get("min_select", 0))
                    max_sel = int(spec.get("max_select", max(1, min_sel)))
                    ksel = rng.randint(min_sel, max_sel) if max_sel > 0 else 0
                    pool = items[:]
                    picked: List[Any] = []
                    for _ in range(min(ksel, len(pool))):
                        v = _weighted_choice(rng, pool)
                        picked.append(v)
                        pool = [(vv, ww) for (vv, ww) in pool if vv != v]
                        if not pool:
                            break
                    out[row] = picked
            return out

        if not multi:
            val = _weighted_choice(rng, items)
            label = str(field.get("label") or field.get("label_text") or "")
            _maybe_sync_persona_gender_from_field(field, label, val, state, tuning)
            return val

        min_sel = int(spec.get("min_select", 0))
        max_sel = int(spec.get("max_select", max(1, min_sel)))
        max_sel = max(min_sel, max_sel)
        ksel = rng.randint(min_sel, max_sel) if max_sel > 0 else 0
        pool = items[:]
        picked: List[Any] = []
        for _ in range(min(ksel, len(pool))):
            v = _weighted_choice(rng, pool)
            picked.append(v)
            pool = [(vv, ww) for (vv, ww) in pool if vv != v]
            if not pool:
                break
        return picked

    return None


def build_planned_values(
    config: Dict[str, Any],
    state: RespondentState,
    tuning: Dict[str, Any],
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    planned: Dict[str, Any] = {}
    field_by_ident: Dict[str, Any] = {}

    fields = config.get("fields", []) or []
    sorted_fields = sorted(
        fields,
        key=lambda f: get_fill_priority(normalize_question_identity(str(f.get("label") or f.get("label_text") or ""))),
    )
    for f in sorted_fields:
        label = str(f.get("label") or f.get("label_text") or "")
        ident = normalize_question_identity(label)
        field_by_ident[ident] = f
        val = generate_field_value(f, state, tuning)
        if val is not None:
            planned[ident] = val

    return planned, field_by_ident


# -----------------------------
# Solver models
# -----------------------------


@dataclass
class GridExtract:
    rows: List[str] = field(default_factory=list)
    columns: List[str] = field(default_factory=list)
    kind: str = ""  # "mc"|"checkbox"
    required_per_row: bool = False


@dataclass
class BlockInfo:
    block: Any
    label_text: str
    help_text: str
    kind: str
    required: bool
    error_text: str
    options: List[str] = field(default_factory=list)
    allow_other: bool = False
    grid: Optional[GridExtract] = None
    ident: str = ""


@dataclass
class StepContext:
    signature: str
    quick_sig: str
    blocks: List[BlockInfo]
    nav_labels: List[str]
    visible_questions: List[str]


# -----------------------------
# ConstraintSolver
# -----------------------------


class ConstraintSolver:
    _PROBE_MS = 250
    _NAV_POLL_TOTAL_S = 6.0
    _NAV_POLL_SLEEP_S = 0.2

    def __init__(
        self,
        page,
        timeout_ms: int = 30000,
        diagnostics_dir: Path = Path("runner_diagnostics"),
        learning_store: Dict[str, Any] = {},
        signature_repeat_max: int = 10,
    ) -> None:
        self.page = page
        self.timeout_ms = timeout_ms
        self.diagnostics_dir = diagnostics_dir
        self.learning_store = learning_store
        self.signature_repeat_max = signature_repeat_max
        self._sig_counts: Dict[str, int] = {}
        self._repair_attempts: Dict[str, Set[str]] = {}

    # -------- safe probes --------

    def _safe_inner_text(self, loc, timeout_ms: int = _PROBE_MS) -> str:
        try:
            return normalize_button_label(loc.inner_text(timeout=timeout_ms))
        except Exception:
            return ""

    def _safe_input_value(self, loc, timeout_ms: int = _PROBE_MS) -> str:
        try:
            return (loc.input_value(timeout=timeout_ms) or "").strip()
        except Exception:
            return ""

    def _safe_attr(self, loc, name: str, timeout_ms: int = _PROBE_MS) -> str:
        try:
            return (loc.get_attribute(name, timeout=timeout_ms) or "").strip()
        except Exception:
            return ""

    # -------- container --------

    def form_container(self) -> Any:
        for sel in ['div[role="main"]', "form"]:
            loc = self.page.locator(sel).first
            try:
                if loc.count() > 0 and loc.is_visible(timeout=self._PROBE_MS):
                    return loc
            except Exception:
                pass
        return self.page.locator("body")

    # -------- fast nav state --------

    def nav_button_labels(self) -> List[str]:
        try:
            labels = self.page.eval_on_selector_all("button, [role='button']", "els => els.map(e => e.innerText)")
            out = [normalize_button_label(t) for t in labels if t]
        except Exception:
            out = []
        return [c for c in out if c.lower() in {"next", "submit", "back", "continue", "proceed"}]

    def _body_hash8(self) -> str:
        try:
            s = self.page.evaluate(
                "() => (document.body && document.body.innerText ? document.body.innerText.slice(0, 2000) : '')"
            )
            s = " ".join(str(s or "").split())
            if not s:
                return ""
            return hashlib.md5(s.encode("utf-8")).hexdigest()[:8]
        except Exception:
            return ""

    def quick_state(self, nav_labels: Optional[List[str]] = None) -> str:
        labels = nav_labels if nav_labels is not None else self.nav_button_labels()
        joined = "|".join(labels)
        return f"{joined}#{self._body_hash8()}"

    def poll_nav_button_labels(
        self,
        *,
        total_ms: int = 8000,
        sleep_ms: int = 200,
        require_any: Optional[List[str]] = None,
    ) -> List[str]:
        require_any = require_any or ["Next", "Back", "Submit"]
        require_set = {x.lower() for x in require_any}

        end = time.time() + (total_ms / 1000.0)
        esc_sent = False
        last: List[str] = []

        while time.time() < end:
            if not esc_sent:
                try:
                    opt = self.page.locator('[role="option"]').first
                    if opt.count() > 0 and opt.is_visible(timeout=100):
                        self.page.keyboard.press("Escape")
                        esc_sent = True
                except Exception:
                    pass

            labels = self.nav_button_labels()
            last = labels
            if labels and any(l.lower() in require_set for l in labels):
                return labels

            time.sleep(max(0.05, sleep_ms / 1000.0))

        return last

    # -------- extraction --------

    def _extract_label(self, block) -> str:
        candidates: List[str] = []

        def _push(txt: str) -> None:
            t = re.sub(r"\*\s*$", "", normalize_button_label(txt)).strip()
            if t and t not in candidates:
                candidates.append(t)

        for sel in [
            ".freebirdFormviewerViewItemsItemItemTitle",
            "[data-question-title]",
            "[data-params*='title']",
        ]:
            try:
                loc = block.locator(sel).first
                if loc.count() > 0:
                    txt = self._safe_inner_text(loc)
                    if txt:
                        _push(txt)
            except Exception:
                pass

        for sel in [
            "[role='heading'][aria-level='3']",
            "[role='heading'][aria-level='2']",
            "[role='heading'][aria-level='1']",
            "[role='heading']",
        ]:
            try:
                loc = block.locator(sel).first
                if loc.count() > 0:
                    txt = self._safe_inner_text(loc)
                    if txt:
                        _push(txt)
            except Exception:
                pass

        try:
            aria_label = self._safe_attr(block, "aria-label")
            if aria_label:
                _push(aria_label)
        except Exception:
            pass

        try:
            labelled_by = self._safe_attr(block, "aria-labelledby")
            if labelled_by:
                for part in labelled_by.split():
                    loc = self.page.locator(f"#{part}").first
                    if loc.count() > 0:
                        txt = self._safe_inner_text(loc)
                        if txt:
                            _push(txt)
        except Exception:
            pass

        if not candidates:
            try:
                locs = block.locator("div[dir='auto']")
                n = min(6, locs.count())
                for i in range(n):
                    loc = locs.nth(i)
                    txt = self._safe_inner_text(loc)
                    if not txt:
                        continue
                    low = txt.strip().lower()
                    if low in {"other", "other:"}:
                        continue
                    _push(txt)
                    break
            except Exception:
                pass

        return candidates[0] if candidates else ""

    def _extract_help_text(self, block) -> str:
        loc = block.locator(
            ".freebirdFormviewerViewItemsItemItemHelpText, [aria-label*='help'], div[dir='auto']:nth-child(2)"
        )
        if loc.count() == 0:
            return ""
        return self._safe_inner_text(loc.first)

    def _detect_required(self, block, label_text: str) -> bool:
        try:
            if block.locator("[aria-label*='Required'], [aria-label*='required']").count() > 0:
                return True
        except Exception:
            pass
        return bool(re.search(r"\*\s*$", label_text))

    def _extract_error_text(self, block) -> str:
        loc = block.locator('[role="alert"], [aria-live="assertive"], [aria-live="polite"]')
        best = ""
        best_score = -1
        try:
            n = loc.count()
            for i in range(n):
                it = loc.nth(i)
                try:
                    if not it.is_visible(timeout=self._PROBE_MS):
                        continue
                except Exception:
                    continue
                txt = self._safe_inner_text(it)
                if not txt:
                    continue
                score = sum(
                    2
                    for k in (
                        "required",
                        "must",
                        "valid",
                        "match",
                        "pattern",
                        "format",
                        "email",
                        "number",
                        "exactly",
                        "row",
                    )
                    if k in txt.lower()
                )
                if score > best_score:
                    best_score = score
                    best = txt
        except Exception:
            pass
        return best

    def _extract_radio_options(self, block) -> Tuple[List[str], bool]:
        out: List[str] = []
        allow_other = False
        radios = block.locator("[role='radio']")
        try:
            n = radios.count()
        except Exception:
            n = 0
        for i in range(n):
            r = radios.nth(i)
            try:
                if not r.is_visible(timeout=self._PROBE_MS):
                    continue
            except Exception:
                continue
            label = self._safe_attr(r, "aria-label") or self._safe_inner_text(r)
            if not label:
                continue
            label = normalize_button_label(label)
            if is_other_option(label):
                allow_other = True
                continue
            if label not in out:
                out.append(label)
        return out, allow_other

    def _extract_checkbox_options(self, block) -> Tuple[List[str], bool]:
        out: List[str] = []
        allow_other = False
        cbs = block.locator("[role='checkbox']")
        try:
            n = cbs.count()
        except Exception:
            n = 0
        for i in range(n):
            c = cbs.nth(i)
            try:
                if not c.is_visible(timeout=self._PROBE_MS):
                    continue
            except Exception:
                continue
            label = self._safe_attr(c, "aria-label") or self._safe_inner_text(c)
            if not label:
                continue
            label = normalize_button_label(label)
            if is_other_option(label):
                allow_other = True
                continue
            if label not in out:
                out.append(label)
        return out, allow_other

    # -------- grid detection (new) --------

    def _infer_grid_required_per_row(
        self, required: bool, error_text: str, gkind: str
    ) -> bool:
        if not required:
            return False
        err_low = error_text.lower()
        if any(k in err_low for k in ["each row", "every row", "per row"]):
            return True
        if any(k in err_low for k in ["at least one", "one row"]):
            return False
        if gkind == "mc":
            return True
        return False

    def _extract_grid_info(
        self, block, error_text: str = "", required: bool = False
    ) -> Optional[GridExtract]:
        """Detect MC/Checkbox grid using ARIA heuristics (same as wizard). Structural first, then aria-label fallback."""

        # Structural first (role=grid / table)
        for grid_sel in ['[role="grid"]', "table"]:
            g = block.locator(grid_sel).first
            if g.count() > 0:
                rows_loc = block.locator('[role="rowheader"]')
                cols_loc = block.locator('[role="columnheader"]')
                rows: List[str] = []
                for i in range(min(20, rows_loc.count())):
                    txt = self._safe_inner_text(rows_loc.nth(i))
                    if txt:
                        rows.append(txt)
                cols: List[str] = []
                for i in range(min(20, cols_loc.count())):
                    txt = self._safe_inner_text(cols_loc.nth(i))
                    if txt:
                        cols.append(txt)
                if len(rows) >= 2 and len(cols) >= 2:
                    has_radio = block.locator("[role='radio']").count() > 0
                    gkind = "mc" if has_radio else "checkbox"
                    required_per_row = self._infer_grid_required_per_row(
                        required, error_text, gkind
                    )
                    return GridExtract(
                        rows=rows, columns=cols, kind=gkind, required_per_row=required_per_row
                    )

        # ARIA fallback (Google Forms style)
        cells = block.locator("[role='radio'], [role='checkbox']")
        try:
            n = cells.count()
        except Exception:
            n = 0
        if n < 6:
            return None

        # determine kind once from first cell
        gkind = "checkbox"
        if n > 0:
            try:
                first_role = self._safe_attr(cells.nth(0), "role", timeout_ms=100)
                if first_role == "radio":
                    gkind = "mc"
            except Exception:
                pass

        row_set: Set[str] = set()
        col_set: Set[str] = set()
        rows: List[str] = []
        cols: List[str] = []

        for i in range(n):
            c = cells.nth(i)
            aria = self._safe_attr(c, "aria-label", timeout_ms=100) or ""
            if not aria:
                continue

            # parse row / col using common separators
            row_part = col_part = ""
            a = b = ""
            if "," in aria:
                parts = aria.split(",", 1)
                a, b = parts[0].strip(), parts[1].strip()
            elif " - " in aria:
                parts = aria.split(" - ", 1)
                a, b = parts[0].strip(), parts[1].strip()
            elif ":" in aria:
                parts = aria.split(":", 1)
                a, b = parts[0].strip(), parts[1].strip()
            else:
                continue

            b_low = b.lower()
            if b_low.startswith("response for "):
                col_part = a.strip()
                row_part = b[len("response for ") :].strip()
            else:
                row_part = a.strip()
                col_part = b.strip()

            if row_part and row_part not in row_set:
                row_set.add(row_part)
                rows.append(row_part)
            if col_part and col_part not in col_set:
                col_set.add(col_part)
                cols.append(col_part)

        if len(rows) >= 2 and len(cols) >= 2:
            required_per_row = self._infer_grid_required_per_row(
                required, error_text, gkind
            )
            return GridExtract(
                rows=rows, columns=cols, kind=gkind, required_per_row=required_per_row
            )
        return None

    # -------- detectors (bounded + exists check) --------

    def _detect_file_upload(self, block) -> bool:
        loc = block.locator("input[type='file']")
        if loc.count() == 0:
            return False
        try:
            return loc.first.is_visible(timeout=self._PROBE_MS)
        except Exception:
            return False

    def _detect_date_kind(self, block) -> bool:
        loc = block.locator('input[type="date"]')
        if loc.count() > 0:
            try:
                if loc.first.is_visible(timeout=self._PROBE_MS):
                    return True
            except Exception:
                pass

        tb = block.get_by_role("textbox")
        if tb.count() == 0:
            return False
        try:
            ph = tb.first.get_attribute("placeholder", timeout=self._PROBE_MS) or ""
            return "date" in ph.lower()
        except Exception:
            return False

    def _detect_time_kind(self, block) -> bool:
        loc = block.locator('input[type="time"]')
        if loc.count() > 0:
            try:
                if loc.first.is_visible(timeout=self._PROBE_MS):
                    return True
            except Exception:
                pass

        tb = block.get_by_role("textbox")
        if tb.count() == 0:
            return False
        try:
            ph = tb.first.get_attribute("placeholder", timeout=self._PROBE_MS) or ""
            return "time" in ph.lower()
        except Exception:
            return False

    # -------- dropdown primitive --------

    def _wait_listbox_expanded(self, lb) -> None:
        end = time.time() + 0.8
        while time.time() < end:
            try:
                val = (lb.get_attribute("aria-expanded", timeout=self._PROBE_MS) or "").strip().lower()
                if val == "true":
                    return
            except Exception:
                pass
            time.sleep(0.05)

    def _wait_options_visible(self, lb) -> bool:
        end = time.time() + 1.5
        while time.time() < end:
            opts = lb.locator('div[role="option"]')
            try:
                if opts.count() > 0 and opts.first.is_visible(timeout=100):
                    return True
            except Exception:
                pass
            time.sleep(0.1)
        return False

    def _read_listbox_selected(self, lb) -> Tuple[str, str]:
        opt = lb.locator('div[role="option"][aria-selected="true"]').first
        if opt.count() == 0:
            return "", ""
        dv = self._safe_attr(opt, "data-value")
        span = opt.locator("span").first
        txt = self._safe_inner_text(span) if span.count() > 0 else self._safe_inner_text(opt)
        return (dv or "").strip(), (txt or "").strip()

    def select_dropdown_listbox_dom(
        self,
        block,
        choice: str,
        *,
        required: bool,
        allow_other: bool,
        label: str,
    ) -> None:
        choice = normalize_button_label(str(choice or ""))
        if not choice:
            return
        if is_other_option(choice) and not allow_other:
            return

        lb = block.locator('div[role="listbox"]').first
        if lb.count() == 0:
            return

        def open_menu() -> None:
            try:
                lb.scroll_into_view_if_needed(timeout=800)
            except Exception:
                pass
            try:
                lb.click(timeout=1500)
            except Exception:
                lb.click(timeout=1500, force=True)
            self._wait_listbox_expanded(lb)
            self._wait_options_visible(lb)

        def click_option() -> None:
            opt = lb.locator(f'div[role="option"][data-value="{choice}"]').first
            if opt.count() == 0:
                raise RuntimeError(f"Option not found in listbox by data-value: {choice!r}")
            try:
                opt.scroll_into_view_if_needed(timeout=800)
            except Exception:
                pass
            try:
                opt.click(timeout=1500, force=True)
            except Exception as e:
                if "outside of the viewport" in str(e):
                    try:
                        opt.evaluate("el => el.click()")
                    except Exception:
                        raise RuntimeError(f"Failed to click dropdown option: {label!r} -> {choice!r}")
                else:
                    raise

        last_selected = ""
        try:
            for attempt in range(2):
                open_menu()
                click_option()
                dv, txt = self._read_listbox_selected(lb)
                last_selected = txt or dv or ""
                if dv == choice or txt == choice:
                    return
                if attempt == 0:
                    continue
        finally:
            try:
                self.page.keyboard.press("Escape")
            except Exception:
                pass

        if required:
            self.dump_diagnostics("dropdown_required_not_selected")
            raise RuntimeError(f"Required dropdown not selected: {label!r} -> {choice!r} (selected={last_selected!r})")

    # -------- robust radio click --------

    def _resolve_radio_target(self, block, desired: str) -> Any:
        desired = normalize_button_label(desired)
        if not desired:
            return None

        exact = block.get_by_role("radio", name=desired, exact=True)
        try:
            if exact.count() > 0 and exact.first.is_visible(timeout=self._PROBE_MS):
                return exact.first
        except Exception:
            pass

        desired_low = desired.lower()
        radios = block.locator("[role='radio']")
        try:
            n = radios.count()
        except Exception:
            n = 0

        for i in range(n):
            r = radios.nth(i)
            try:
                if not r.is_visible(timeout=self._PROBE_MS):
                    continue
            except Exception:
                continue
            label = self._safe_attr(r, "aria-label", timeout_ms=300) or self._safe_inner_text(r, timeout_ms=300)
            if desired_low in normalize_button_label(label).lower():
                return r

        return None

    def _poll_choice_enabled(self, el, timeout_ms: int = 8000) -> bool:
        end = time.time() + (timeout_ms / 1000.0)
        while time.time() < end:
            try:
                aria_disabled = (self._safe_attr(el, "aria-disabled", timeout_ms=300) or "").strip().lower()
                if aria_disabled != "true":
                    return True
            except Exception:
                pass
            time.sleep(0.1)
        return False

    def _is_checked(self, el) -> bool:
        try:
            if (self._safe_attr(el, "aria-checked") or "").strip().lower() == "true":
                return True
            cls = self._safe_attr(el, "class") or ""
            if "N2RpBe" in cls:
                return True
        except Exception:
            pass
        return False

    def _ensure_checked(self, el, block: Any = None, choice: str = "", timeout_ms=800) -> bool:
        if self._is_checked(el):
            return True
        attempts = [
            lambda: el.click(timeout=1500),
            lambda: el.click(timeout=1500, force=True),
            lambda: el.locator("xpath=ancestor::*[self::label or self::div][1]").first.click(timeout=1500, force=True),
            lambda: el.locator("xpath=ancestor::div[@role='radio' or @role='presentation'][1]").first.click(timeout=1500, force=True),
            lambda: el.evaluate("e => e.click()"),
        ]
        if block and choice:
            attempts.append(lambda: block.get_by_text(choice, exact=True).click(timeout=1500, force=True))
        for attempt in attempts:
            try:
                attempt()
            except Exception:
                pass
            end_poll = time.time() + (timeout_ms / 1000.0)
            while time.time() < end_poll:
                if self._is_checked(el):
                    return True
                time.sleep(0.05)
        return False

    def _poll_radio_checked(self, radio, *, total_ms: int = 800) -> bool:
        end = time.time() + (total_ms / 1000.0)
        while time.time() < end:
            if self._is_checked(radio):
                return True
            time.sleep(0.1)
        return False

    def click_radio_choice(
        self,
        block,
        choice: str,
        *,
        required: bool,
        allow_other: bool,
        label: str,
    ) -> str:
        choice = normalize_button_label(str(choice or ""))
        if not choice:
            return ""

        if is_other_option(choice) and not allow_other:
            return ""

        radio = self._resolve_radio_target(block, choice)
        if radio is None:
            if not required:
                return ""
            # For required, try to find any radio as fallback later

        enabled = self._poll_choice_enabled(radio)
        if enabled:
            try:
                radio.scroll_into_view_if_needed(timeout=800)
            except Exception:
                pass

            ok = self._ensure_checked(radio, block=block, choice=choice)
            if ok:
                picked = self._safe_attr(radio, "aria-label", timeout_ms=300) or self._safe_inner_text(radio, timeout_ms=300)
                return normalize_button_label(picked) or choice

        if not required:
            return ""

        # Fallback for required
        radios = block.locator("[role='radio']")
        try:
            n = radios.count()
        except Exception:
            n = 0
        for i in range(n):
            r = radios.nth(i)
            try:
                if not r.is_visible(timeout=self._PROBE_MS):
                    continue
            except Exception:
                continue
            opt_txt = self._safe_attr(r, "aria-label", timeout_ms=300) or self._safe_inner_text(r, timeout_ms=300)
            if (not allow_other) and is_other_option(opt_txt):
                continue
            if not self._poll_choice_enabled(r):
                continue
            try:
                r.scroll_into_view_if_needed(timeout=800)
            except Exception:
                pass
            ok = self._ensure_checked(r, block=block, choice=normalize_button_label(opt_txt))
            if ok:
                fallback_choice = normalize_button_label(opt_txt) or choice
                print(f"RADIO FALLBACK: {label!r} requested={choice!r} picked={fallback_choice}")
                return fallback_choice

        self.dump_diagnostics("radio_required_no_enabled")
        raise RuntimeError(f"No enabled radio option for required: {label!r}")

    def _resolve_checkbox_target(self, block, desired: str) -> Any:
        desired = normalize_button_label(desired)
        if not desired:
            return None

        exact = block.get_by_role("checkbox", name=desired, exact=True)
        try:
            if exact.count() > 0 and exact.first.is_visible(timeout=self._PROBE_MS):
                return exact.first
        except Exception:
            pass

        desired_low = desired.lower()
        cbs = block.locator("[role='checkbox']")
        try:
            n = cbs.count()
        except Exception:
            n = 0

        for i in range(n):
            c = cbs.nth(i)
            try:
                if not c.is_visible(timeout=self._PROBE_MS):
                    continue
            except Exception:
                continue
            label = self._safe_attr(c, "aria-label", timeout_ms=300) or self._safe_inner_text(c, timeout_ms=300)
            if desired_low in normalize_button_label(label).lower():
                return c

        return None

    def click_checkbox_choice(
        self,
        block,
        choice: str,
        *,
        required: bool,
        allow_other: bool,
        label: str,
    ) -> bool:
        choice = normalize_button_label(str(choice or ""))
        if not choice:
            return False

        if is_other_option(choice) and not allow_other:
            return False

        cb = self._resolve_checkbox_target(block, choice)
        if cb is None:
            if required:
                self.dump_diagnostics("checkbox_target_not_found")
                raise RuntimeError(f"Required checkbox option not found: {label!r} -> {choice!r}")
            return False

        if not self._poll_choice_enabled(cb):
            if required:
                raise RuntimeError(f"Required checkbox disabled: {label!r} -> {choice!r}")

        try:
            cb.scroll_into_view_if_needed(timeout=800)
        except Exception:
            pass

        ok = self._ensure_checked(cb, block=block, choice=choice)
        if ok:
            return True

        if required:
            self.dump_diagnostics("checkbox_required_not_checked")
            raise RuntimeError(f"Required checkbox not checked: {label!r} -> {choice!r}")

        return False

    # -------- grid fill helpers (new) --------

    def _fill_mc_grid(
        self,
        block: Any,
        grid: GridExtract,
        val: Dict[str, str],
        required: bool,
        label: str,
    ) -> None:
        """Fill MC grid by matching aria-label row+col."""
        cells = block.locator("[role='radio']")
        try:
            n_cells = cells.count()
        except Exception:
            n_cells = 0
        for row_label in grid.rows:
            col_label = val.get(row_label)
            if not col_label:
                continue
            target_radio = None
            row_cells = []
            for i in range(n_cells):
                c = cells.nth(i)
                aria = self._safe_attr(c, "aria-label", timeout_ms=100) or ""
                if row_label in aria:
                    row_cells.append(c)
                    if col_label in aria:
                        target_radio = c
            if target_radio is None and row_cells:
                target_radio = row_cells[0]
            if target_radio is None:
                print(f"MC_GRID: no radio for row {row_label}")
                continue
            try:
                target_radio.click(timeout=1500)
            except Exception:
                try:
                    target_radio.click(timeout=1500, force=True)
                except Exception:
                    pass
            # Verify row has selection
            has_selected = False
            for c in row_cells:
                if self._is_checked(c):
                    has_selected = True
                    break
            if not has_selected:
                # Retry
                try:
                    target_radio.click(timeout=1500, force=True)
                except Exception:
                    pass
                for c in row_cells:
                    if self._is_checked(c):
                        has_selected = True
                        break
            if not has_selected and row_cells:
                # Fallback to first
                first_cell = row_cells[0]
                try:
                    first_cell.click(timeout=1500, force=True)
                except Exception:
                    pass
                print(f"MC_GRID FALLBACK: selected first for {row_label}")
        self._repair_grid_if_needed(block, grid, "mc", required)

    def _fill_checkbox_grid(
        self,
        block: Any,
        grid: GridExtract,
        val: Dict[str, List[str]],
        required: bool,
        label: str,
    ) -> None:
        """Fill checkbox grid by matching aria-label row+col."""
        cells = block.locator("[role='checkbox']")
        try:
            n_cells = cells.count()
        except Exception:
            n_cells = 0
        for row_label in grid.rows:
            cols_sel = val.get(row_label, [])
            if not isinstance(cols_sel, list):
                cols_sel = [cols_sel] if cols_sel else []
            picked_any = False
            for col_label in cols_sel:
                for i in range(n_cells):
                    c = cells.nth(i)
                    aria = self._safe_attr(c, "aria-label", timeout_ms=100) or ""
                    if row_label in aria and col_label in aria:
                        try:
                            if not self._is_checked(c):
                                c.click(timeout=1500)
                            picked_any = True
                            break
                        except Exception:
                            pass
            if not picked_any and grid.required_per_row and required:
                # immediate repair
                if grid.columns:
                    first_col = grid.columns[0]
                    for i in range(n_cells):
                        c = cells.nth(i)
                        aria = self._safe_attr(c, "aria-label", timeout_ms=100) or ""
                        if row_label in aria and first_col in aria:
                            try:
                                c.click(timeout=1500)
                                break
                            except Exception:
                                pass
                            break
        self._repair_grid_if_needed(block, grid, "checkbox", required)

    def _repair_grid_if_needed(
        self, block: Any, grid: GridExtract, gkind: str, required: bool
    ) -> None:
        """Quick DOM check + repair for required_per_row."""
        if not (required and grid.required_per_row):
            return
        role = "radio" if gkind == "mc" else "checkbox"
        cells = block.locator(f"[role='{role}']")
        try:
            n_cells = min(50, cells.count())
        except Exception:
            n_cells = 0
        missing_rows = []
        for row_label in grid.rows:
            has_sel = False
            for i in range(n_cells):
                c = cells.nth(i)
                aria = self._safe_attr(c, "aria-label", timeout_ms=100) or ""
                if row_label in aria and self._is_checked(c):
                    has_sel = True
                    break
            if not has_sel:
                missing_rows.append(row_label)
        for row_label in missing_rows:
            # repair: pick first column
            if grid.columns:
                first_col = grid.columns[0]
                for i in range(n_cells):
                    c = cells.nth(i)
                    aria = self._safe_attr(c, "aria-label", timeout_ms=100) or ""
                    if row_label in aria and first_col in aria:
                        try:
                            c.click(timeout=1500, force=True)
                            print(f"GRID REPAIR: {gkind} row {row_label}")
                            break
                        except Exception:
                            pass
                        break

    def note_signature(self, sig: str) -> None:
        if not sig:
            return
        self._sig_counts[sig] = self._sig_counts.get(sig, 0) + 1
        if self._sig_counts[sig] > self.signature_repeat_max:
            self.dump_diagnostics("loop_protection")
            raise RuntimeError("Signature repeat limit exceeded (loop protection)")

    def dump_diagnostics(self, context: str) -> None:
        self.diagnostics_dir.mkdir(exist_ok=True)
        ts = int(time.time())
        base = f"{context}_{ts}"
        try:
            self.page.screenshot(path=str(self.diagnostics_dir / f"{base}.png"))
        except Exception:
            pass
        try:
            (self.diagnostics_dir / f"{base}.html").write_text(self.page.content(), encoding="utf-8")
        except Exception:
            pass
        meta = {"nav_labels": self.nav_button_labels(), "quick_sig": self.quick_state()}
        try:
            (self.diagnostics_dir / f"{base}.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
        except Exception:
            pass
        print(f"Diagnostics dumped: {base} (png/html/json)")

    def build_step_context(self) -> StepContext:
        nav_labels = self.nav_button_labels()
        if not nav_labels:
            nav_labels = self.poll_nav_button_labels()
        quick_sig = self.quick_state(nav_labels=nav_labels)
        blocks = self.extract_visible_blocks()
        signature = self.compute_signature_from_blocks(blocks, nav_labels, quick_sig)
        visible_questions = [b.label_text for b in blocks]
        return StepContext(
            signature=signature,
            quick_sig=quick_sig,
            blocks=blocks,
            nav_labels=nav_labels,
            visible_questions=visible_questions,
        )

    def extract_visible_blocks(self) -> List[BlockInfo]:
        container = self.form_container()
        blocks_out: List[BlockInfo] = []

        selectors = [
            'div[role="listitem"]',
            "div.freebirdFormviewerViewItemsItemItem",
            "div[jscontroller][data-item-id]",
        ]

        chosen = None
        for sel in selectors:
            locs = container.locator(sel)
            try:
                if locs.count() > 0:
                    chosen = locs
                    break
            except Exception:
                continue

        if chosen is None:
            return []

        try:
            n = chosen.count()
        except Exception:
            n = 0

        for i in range(n):
            b = chosen.nth(i)
            try:
                if not b.is_visible(timeout=self._PROBE_MS):
                    continue
            except Exception:
                continue

            label = self._extract_label(b) or f"(unnamed question {i + 1})"
            help_text = self._extract_help_text(b)
            required = self._detect_required(b, label)
            err = self._extract_error_text(b)
            bi = self._classify_block(b, label, help_text, required, err)
            bi.ident = normalize_question_identity(label)
            blocks_out.append(bi)

        return blocks_out

    def compute_signature_from_blocks(self, blocks: List[BlockInfo], nav_labels: List[str], quick_sig: str) -> str:
        top = [normalize_button_label(b.label_text) for b in blocks[:3] if b.label_text]
        if top:
            return "Q:" + "||".join(top)
        if nav_labels:
            return "NAV:" + "|".join(nav_labels)
        return quick_sig or ""

    def _classify_block(self, block, label: str, help_text: str, required: bool, error_text: str) -> BlockInfo:
        if self._detect_file_upload(block):
            return BlockInfo(
                block=block,
                label_text=label,
                help_text=help_text,
                kind="file_upload",
                required=required,
                error_text=error_text,
            )

        if self._detect_date_kind(block):
            return BlockInfo(block=block, label_text=label, help_text=help_text, kind="date", required=required, error_text=error_text)

        if self._detect_time_kind(block):
            return BlockInfo(block=block, label_text=label, help_text=help_text, kind="time", required=required, error_text=error_text)

        # GRID DETECTION (before radio/checkbox to avoid misclassification)
        grid_info = self._extract_grid_info(block, error_text, required)
        if grid_info:
            k = "mc_grid" if grid_info.kind == "mc" else "checkbox_grid"
            return BlockInfo(
                block=block,
                label_text=label,
                help_text=help_text,
                kind=k,
                required=required,
                error_text=error_text,
                grid=grid_info,
            )

        if block.locator("[role='radio']").count() > 0:
            opts, allow_other = self._extract_radio_options(block)
            return BlockInfo(
                block=block,
                label_text=label,
                help_text=help_text,
                kind="radio",
                required=required,
                error_text=error_text,
                options=opts,
                allow_other=allow_other,
            )

        if block.locator("[role='checkbox']").count() > 0:
            opts, allow_other = self._extract_checkbox_options(block)
            return BlockInfo(
                block=block,
                label_text=label,
                help_text=help_text,
                kind="checkbox",
                required=required,
                error_text=error_text,
                options=opts,
                allow_other=allow_other,
            )

        if block.locator('div[role="listbox"], [role="combobox"]').count() > 0:
            return BlockInfo(block=block, label_text=label, help_text=help_text, kind="dropdown", required=required, error_text=error_text)

        if block.locator("textarea").count() > 0:
            return BlockInfo(block=block, label_text=label, help_text=help_text, kind="paragraph", required=required, error_text=error_text)

        if block.get_by_role("textbox").count() > 0 or block.locator("input[type='text'], input:not([type])").count() > 0:
            return BlockInfo(block=block, label_text=label, help_text=help_text, kind="text", required=required, error_text=error_text)

        return BlockInfo(block=block, label_text=label, help_text=help_text, kind="unknown", required=required, error_text=error_text)

    def _find_nav_button(self, label: str):
        low_label = label.lower()
        btns = self.page.locator("button, [role='button']")
        try:
            n = btns.count()
        except Exception:
            n = 0
        for i in range(n):
            b = btns.nth(i)
            try:
                if not b.is_visible(timeout=self._PROBE_MS):
                    continue
            except Exception:
                continue
            txt = normalize_button_label(self._safe_inner_text(b)).lower()
            if txt == low_label:
                return b
        return None

    def is_next_visible(self) -> bool:
        return self._find_nav_button("Next") is not None

    def is_submit_visible(self) -> bool:
        return self._find_nav_button("Submit") is not None

    def is_terminal(self) -> bool:
        return self.is_submit_visible() and not self.is_next_visible()

    def click_nav(self, label: str, timeout_ms: int = 2000, no_wait_after: bool = False) -> None:
        btn = self._find_nav_button(label)
        if not btn:
            raise RuntimeError(f"Nav button not found: {label}")
        try:
            btn.scroll_into_view_if_needed(timeout=800)
        except Exception:
            pass
        btn.click(timeout=timeout_ms, no_wait_after=no_wait_after)
        print(f"NAV: clicked {label}")

    def _poll_editable_locator(self, get_loc_func: Callable[[], Any], total_ms=2500, sleep_ms=80) -> Optional[Any]:
        end = time.time() + total_ms / 1000.0
        focused = False
        while time.time() < end:
            fresh = get_loc_func()
            if fresh.count() == 0:
                time.sleep(sleep_ms / 1000.0)
                continue
            try:
                if not focused:
                    fresh.click(timeout=800)
                    focused = True
            except Exception:
                pass
            try:
                disabled = fresh.get_attribute("disabled", timeout=300)
                aria_disabled = (fresh.get_attribute("aria-disabled", timeout=300) or "").strip().lower()
                visible = fresh.is_visible(timeout=300)
                enabled = fresh.is_enabled(timeout=300)
                if disabled is None and aria_disabled != "true" and visible and enabled:
                    return fresh
            except Exception:
                pass
            time.sleep(sleep_ms / 1000.0)
        return None

    def fill_visible_questions_with_blocks(
        self,
        blocks: List[BlockInfo],
        planned: Dict[str, Any],
        field_by_ident: Dict[str, Any],
        state: RespondentState,
        tuning: Dict[str, Any],
    ) -> None:
        sorted_blocks = sorted(blocks, key=lambda b: get_fill_priority(b.ident))
        for b in sorted_blocks:
            ident = b.ident
            f = field_by_ident.get(ident, {})
            val = planned.get(ident)

            if val is None:
                gen = f.get("generation") or {}
                if str(gen.get("mode") or "").upper() == "SKIP":
                    continue
                val = generate_field_value(f, state, tuning)
                if val is not None:
                    planned[ident] = val

            if val is None:
                continue

            try:
                if b.kind == "file_upload":
                    if b.required:
                        raise RuntimeError(f"Required file upload not supported: {b.label_text}")
                    continue

                if b.kind in {"text", "paragraph"}:
                    def get_tb():
                        return b.block.get_by_role("textbox").first if b.kind == "text" else b.block.locator("textarea").first

                    tb = self._poll_editable_locator(get_tb, total_ms=8000 if b.required else 2500)
                    if tb:
                        try:
                            tb.click(timeout=1500)
                        except Exception:
                            pass
                        tb.fill(str(val), timeout=min(self.timeout_ms, 4000))
                        print(f"FILL: {b.label_text} -> {val}")
                        state["_last_answers"][ident] = val
                    elif b.required:
                        self.dump_diagnostics("textbox_not_editable")
                        raise RuntimeError(f"Required textbox not editable: {b.label_text}")
                    continue

                if b.kind == "date":
                    def get_inp():
                        return b.block.locator("input[type='date']").first

                    inp = self._poll_editable_locator(get_inp, total_ms=8000 if b.required else 2500)
                    if inp:
                        try:
                            inp.click(timeout=1500)
                        except Exception:
                            pass
                        inp.fill(str(val), timeout=min(self.timeout_ms, 4000))
                        print(f"FILL: {b.label_text} -> {val}")
                        state["_last_answers"][ident] = val
                        continue
                    def get_tb():
                        return b.block.get_by_role("textbox").first

                    tb = self._poll_editable_locator(get_tb, total_ms=8000 if b.required else 2500)
                    if tb:
                        try:
                            tb.click(timeout=1500)
                        except Exception:
                            pass
                        tb.fill(str(val), timeout=min(self.timeout_ms, 4000))
                        print(f"FILL: {b.label_text} -> {val}")
                        state["_last_answers"][ident] = val
                    elif b.required:
                        self.dump_diagnostics("textbox_not_editable")
                        raise RuntimeError(f"Required textbox not editable: {b.label_text}")
                    continue

                if b.kind == "time":
                    def get_inp():
                        return b.block.locator("input[type='time']").first

                    inp = self._poll_editable_locator(get_inp, total_ms=8000 if b.required else 2500)
                    if inp:
                        try:
                            inp.click(timeout=1500)
                        except Exception:
                            pass
                        inp.fill(str(val), timeout=min(self.timeout_ms, 4000))
                        print(f"FILL: {b.label_text} -> {val}")
                        state["_last_answers"][ident] = val
                        continue
                    def get_tb():
                        return b.block.get_by_role("textbox").first

                    tb = self._poll_editable_locator(get_tb, total_ms=8000 if b.required else 2500)
                    if tb:
                        try:
                            tb.click(timeout=1500)
                        except Exception:
                            pass
                        tb.fill(str(val), timeout=min(self.timeout_ms, 4000))
                        print(f"FILL: {b.label_text} -> {val}")
                        state["_last_answers"][ident] = val
                    elif b.required:
                        self.dump_diagnostics("textbox_not_editable")
                        raise RuntimeError(f"Required textbox not editable: {b.label_text}")
                    continue

                if b.kind == "radio":
                    picked = self.click_radio_choice(
                        b.block,
                        str(val),
                        required=b.required,
                        allow_other=b.allow_other,
                        label=b.label_text,
                    )
                    if picked:
                        print(f"FILL: {b.label_text} -> {picked}")
                        state["_last_answers"][ident] = picked
                    if b.required and not picked:
                        raise RuntimeError(f"Required radio not selected: {b.label_text}")
                    continue

                if b.kind == "checkbox":
                    values = val if isinstance(val, list) else [val]
                    if not b.allow_other:
                        values = [v for v in values if not is_other_option(str(v))]
                    picked_values: List[Any] = []
                    for v in values:
                        ok = self.click_checkbox_choice(
                            b.block,
                            str(v),
                            required=False,
                            allow_other=b.allow_other,
                            label=b.label_text,
                        )
                        if ok:
                            picked_values.append(v)
                            print(f"FILL: {b.label_text} -> {v}")

                    checked_count = b.block.locator(
                        "[role='checkbox'][aria-checked='true'], [role='checkbox'][class*='N2RpBe']"
                    ).count()
                    if b.required and checked_count == 0:
                        raise RuntimeError(f"Required checkbox group remained unchecked: {b.label_text}")

                    if picked_values:
                        state["_last_answers"][ident] = picked_values
                    continue

                if b.kind == "dropdown":
                    if is_other_option(str(val)) and (not b.allow_other):
                        continue
                    self.select_dropdown_listbox_dom(
                        b.block,
                        str(val),
                        required=b.required,
                        allow_other=b.allow_other,
                        label=b.label_text,
                    )
                    print(f"FILL: {b.label_text} -> {val}")
                    state["_last_answers"][ident] = val
                    lb = b.block.locator('div[role="listbox"]').first
                    dv, txt = self._read_listbox_selected(lb)
                    if b.required and not (dv or txt):
                        raise RuntimeError(f"Required dropdown not selected: {b.label_text}")
                    continue

                # NEW: grid support
                if b.kind in ("mc_grid", "checkbox_grid"):
                    grid_info = b.grid
                    if grid_info is None:
                        continue
                    val_dict = planned.get(ident)
                    if not isinstance(val_dict, dict):
                        print(f"GRID SKIP: no dict val for {b.label_text}")
                        continue
                    if b.kind == "mc_grid":
                        self._fill_mc_grid(
                            b.block, grid_info, val_dict, b.required, b.label_text
                        )
                    else:
                        self._fill_checkbox_grid(
                            b.block, grid_info, val_dict, b.required, b.label_text
                        )
                    print(f"FILL: grid {b.label_text}")
                    state["_last_answers"][ident] = val_dict
                    continue

            except Exception as e:
                if b.required:
                    raise
                print(f"FILL nonfatal: {b.label_text}: {e}")

    def click_next_with_solver(
        self,
        ctx: StepContext,
        planned: Dict[str, Any],
        field_by_ident: Dict[str, Any],
        state: RespondentState,
        tuning: Dict[str, Any],
    ) -> None:
        self.fill_visible_questions_with_blocks(ctx.blocks, planned, field_by_ident, state, tuning)

        before = ctx.quick_sig
        self.click_nav("Next", timeout_ms=2000, no_wait_after=True)
        self._poll_nav_change(before)

    def click_submit_with_solver(
        self,
        ctx: StepContext,
        planned: Dict[str, Any],
        field_by_ident: Dict[str, Any],
        state: RespondentState,
        tuning: Dict[str, Any],
        success_check,
    ) -> None:
        self.fill_visible_questions_with_blocks(ctx.blocks, planned, field_by_ident, state, tuning)

        before = ctx.quick_sig
        self.click_nav("Submit", timeout_ms=2000, no_wait_after=True)
        self._poll_nav_change(before)

        end = time.time() + 15.0
        while time.time() < end:
            if success_check():
                return
            if "formResponse" in (self.page.url or ""):
                return
            time.sleep(0.2)

        self.dump_diagnostics("submit_no_success")
        raise RuntimeError("Success not detected after submit")

    def _poll_nav_change(self, before_quick: str) -> None:
        end = time.time() + self._NAV_POLL_TOTAL_S
        while time.time() < end:
            labels = self.nav_button_labels()
            quick = self.quick_state(nav_labels=labels)
            if quick and before_quick and quick != before_quick:
                return
            time.sleep(self._NAV_POLL_SLEEP_S)

    def ensure_on_valid_step(self) -> None:
        _ = self.build_step_context()

    def make_block_error_map(self, blocks: List[BlockInfo]) -> Dict[str, str]:
        out: Dict[str, str] = {}
        for b in blocks:
            if b.error_text:
                out[b.ident] = b.error_text
        return out


def make_success_checker(page, success_cfg: Dict[str, Any]):
    patterns = success_cfg.get("success_text") or []
    if isinstance(patterns, str):
        patterns = [patterns]

    patterns = [str(p).strip() for p in patterns if str(p).strip()]

    def check() -> bool:
        if patterns:
            try:
                body = page.evaluate("() => document.body && document.body.innerText ? document.body.innerText : ''")
                body = " ".join(str(body or "").split())
            except Exception:
                body = ""
            low = body.lower()
            for p in patterns:
                if p.lower() in low:
                    return True

        try:
            if page.locator("text=/Your response has been recorded/i").count() > 0:
                return True
        except Exception:
            pass
        return False

    return check


def run_form(
    form_url: str,
    config: Dict[str, Any],
    *,
    headless: bool,
    slow_mo_ms: int,
    timeout_ms: int,
    diagnostics_dir: Path,
    signature_repeat_max: int,
) -> Dict[str, Any]:
    tuning = config.get("tuning", {}) or {}
    state: RespondentState = config.get("persona_state") or {}

    if tuning.get("gender") is not None:
        _apply_gender_override(tuning, state, tuning.get("gender"))

    _ensure_persona_state(tuning, state)

    print(
        f"STATE: gender={state['gender_ui']} name={state['name_full']} email={state['email']} phone={state['phone']} seed={state['rng_seed']}"
    )

    planned, field_by_ident = build_planned_values(config, state, tuning)

    learned = config.get("learned_constraints", {})

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless, slow_mo=slow_mo_ms)
        context = browser.new_context()
        page = context.new_page()
        page.set_default_timeout(timeout_ms)

        solver = ConstraintSolver(
            page,
            timeout_ms=timeout_ms,
            diagnostics_dir=diagnostics_dir,
            learning_store=learned,
            signature_repeat_max=signature_repeat_max,
        )
        success_check = make_success_checker(page, config.get("success", {}))

        try:
            page.goto(form_url, wait_until="domcontentloaded")

            while True:
                ctx = solver.build_step_context()
                solver.note_signature(ctx.signature)

                print(f"NAV buttons: {ctx.nav_labels}")
                print(f"VISIBLE QUESTIONS: {ctx.visible_questions}")

                if solver.is_terminal():
                    solver.click_submit_with_solver(ctx, planned, field_by_ident, state, tuning, success_check)
                    break

                if solver.is_next_visible():
                    ctx = solver.build_step_context()
                    solver.click_next_with_solver(ctx, planned, field_by_ident, state, tuning)
                    continue

                labels = solver.poll_nav_button_labels(total_ms=8000, sleep_ms=200)
                if labels:
                    ctx = solver.build_step_context()
                    print(f"NAV buttons (polled): {ctx.nav_labels}")
                    continue

                solver.dump_diagnostics("no_nav")
                raise RuntimeError("No Next/Submit buttons")

            return {
                "status": "ok",
                "persona_state": {k: v for k, v in state.items() if not k.startswith("_")},
                "learned_constraints": learned,
            }

        except PlaywrightTimeoutError:
            solver.dump_diagnostics("playwright_timeout")
            raise
        finally:
            try:
                context.close()
            except Exception:
                pass
            try:
                browser.close()
            except Exception:
                pass


def run_cmd(args):
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    diagnostics_dir = Path(args.diagnostics_dir)
    diagnostics_dir.mkdir(exist_ok=True)

    # Backward compatible URL resolution (prefer CLI --url).
    url = args.url or config.get("url") or config.get("form_url") or (config.get("meta") or {}).get("url")
    if not url:
        raise SystemExit(
            "runner.py: error: form URL is required. Provide --url or set config['url'] / config['form_url'] / config['meta']['url']."
        )

    # Optional learning-store (persist learned constraints across runs).
    learned_path = args.learned_path or config.get("learned_path")
    if learned_path:
        lp = Path(learned_path)
        if lp.exists():
            try:
                config["learned_constraints"] = json.loads(lp.read_text(encoding="utf-8"))
            except Exception:
                pass

    results = []
    count = max(1, int(getattr(args, "count", 1) or 1))

    for i in range(count):
        if count > 1:
            print(f"\n=== RUN {i+1}/{count} ===")
        results.append(
            run_form(
                url,
                config,
                headless=args.headless,
                slow_mo_ms=args.slow_mo_ms,
                timeout_ms=args.timeout_ms,
                diagnostics_dir=diagnostics_dir,
                signature_repeat_max=args.signature_repeat_max,
            )
        )

    result = results[0] if count == 1 else {"status": "ok", "runs": results}

    if learned_path:
        try:
            Path(learned_path).write_text(json.dumps(result.get("learned_constraints", {}), indent=2), encoding="utf-8")
        except Exception:
            pass

    if args.output:
        Path(args.output).write_text(json.dumps(result, indent=2), encoding="utf-8")
    else:
        print(json.dumps(result, indent=2))


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Google Forms runner (Playwright sync)")

    p.add_argument("--config", required=True, help="Path to JSON config")
    p.add_argument("--url", default="", help="Form URL (overrides config)")
    p.add_argument("--headless", type=str_to_bool, default=True, help="Run headless (true/false)")
    # Backward-compatible aliases:
    p.add_argument("--slow-mo-ms", "--slowmo-ms", dest="slow_mo_ms", type=int, default=0, help="Playwright slowMo (ms)")
    p.add_argument("--timeout-ms", "--timeout", dest="timeout_ms", type=int, default=30000, help="Default timeout (ms)")
    p.add_argument("--diagnostics-dir", default="runner_diagnostics", help="Diagnostics output dir")
    p.add_argument("--count", type=int, default=1, help="Number of runs to execute")
    p.add_argument("--output", default="", help="Write result JSON to this path (optional)")
    p.add_argument("--learned-path", default="", help="Persist learned constraints JSON to this path (optional)")
    p.add_argument("--signature-repeat-max", type=int, default=10, help="Loop protection signature repeat max")

    return p


def main() -> None:
    argv = sys.argv[1:]
    if argv and argv[0].lower() == "run":
        argv = argv[1:]
    args = build_arg_parser().parse_args(argv)
    run_cmd(args)


if __name__ == "__main__":
    main()