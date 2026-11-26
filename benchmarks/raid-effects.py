#!/usr/bin/env python3
"""Emit a local AzerothCore spell-effects fixture or remove its labeled rows."""

import argparse

DESCRIPTION = "Wrath render benchmark fixture"
IDS = range(990500, 990525)
SPELLS = (11113, 1449, 15237, 26573, 6343)
# Three casters per row and two per column, with the original spells and timers.
HALF_RAID = (0, 1, 2, 4, 7, 9, 11, 15, 16, 18, 20, 23, 24)


def values(rows):
    def literal(value):
        return "'" + value.replace("'", "''") + "'" if isinstance(value, str) else str(value)

    return ",\n".join("(" + ",".join(map(literal, row)) + ")" for row in rows)


def create(spawn_entry, casters):
    indices = range(len(IDS)) if casters == 24 else HALF_RAID
    actors = [(index, IDS[index]) for index in indices]
    entries = [
        (entry, "Raid Load Boss" if i == 0 else f"Raid Load Caster {i}", DESCRIPTION, 35,
         "SmartAI", 1, 7, 2000, 1500)
        for i, entry in actors
    ]
    print("START TRANSACTION;")
    print("INSERT INTO creature_template (entry,name,subname,faction,AIName,unit_class,type,BaseAttackTime,RangeAttackTime) VALUES")
    print(values(entries) + ";")
    for index, entry in actors:
        source = 448 if index == 0 else 823
        scale = 1.55 if index == 0 else 1.0
        print(
            "INSERT INTO creature_template_model (CreatureID,Idx,CreatureDisplayID,DisplayScale,Probability)"
            f" SELECT {entry},0,CreatureDisplayID,{scale},1 FROM creature_template_model"
            f" WHERE CreatureID={source} ORDER BY Idx LIMIT 1;"
        )
        ahead = 17 if index == 0 else 9 + 4 * ((index - 1) // 6)
        across = 0 if index == 0 else (-10, -6, -3, 3, 6, 10)[(index - 1) % 6]
        print(
            f"INSERT INTO creature ({spawn_entry},map,zoneId,position_x,position_y,position_z,orientation,Comment)"
            f" SELECT {entry},map,zone,position_x+{ahead}*COS(orientation)-({across})*SIN(orientation),"
            f"position_y+{ahead}*SIN(orientation)+({across})*COS(orientation),position_z,MOD(orientation+PI(),2*PI()),"
            f"'{DESCRIPTION}' FROM acore_characters.characters"
            " WHERE name='Macsmoke' AND online=0 AND map=0 AND zone=12;"
        )
    scripts = []
    for index, entry in actors:
        delay = 350 + 135 * index
        spell = SPELLS[index % len(SPELLS)]
        cycle = 5100 if spell == 26573 else 1500 + 110 * (index % 5)
        scripts.append((entry,0,0,1,delay,delay,cycle,cycle,11,spell,2,1,DESCRIPTION))
    scripts.append((IDS.start,0,1,1,1100,1100,2600,2600,5,36,0,1,DESCRIPTION))
    print("INSERT INTO smart_scripts (entryorguid,source_type,id,event_type,event_param1,event_param2,event_param3,event_param4,action_type,action_param1,action_param2,target_type,comment) VALUES")
    print(values(scripts) + ";")
    print("COMMIT;")


def remove(spawn_entry):
    owned = f"entry BETWEEN {IDS.start} AND {IDS.stop - 1} AND subname='{DESCRIPTION}'"
    print("START TRANSACTION;")
    print(f"DELETE FROM smart_scripts WHERE entryorguid IN (SELECT entry FROM creature_template WHERE {owned}) AND source_type=0 AND comment='{DESCRIPTION}';")
    print(f"DELETE FROM creature WHERE {spawn_entry} IN (SELECT entry FROM creature_template WHERE {owned}) AND Comment='{DESCRIPTION}';")
    print(f"DELETE FROM creature_template_model WHERE CreatureID IN (SELECT entry FROM creature_template WHERE {owned});")
    print(f"DELETE FROM creature_template WHERE {owned};")
    print("COMMIT;")


def check(spawn_entry):
    reserved = f"BETWEEN {IDS.start} AND {IDS.stop - 1}"
    print(f"""
SELECT CONCAT(
 (SELECT COUNT(*) FROM creature_template WHERE entry {reserved}), ',',
 (SELECT COUNT(*) FROM smart_scripts WHERE entryorguid {reserved}), ',',
 (SELECT COUNT(*) FROM creature WHERE {spawn_entry} {reserved}), ',',
 (SELECT COUNT(*) FROM acore_characters.characters WHERE online=1), ',',
 (SELECT COUNT(*) FROM acore_characters.characters
  WHERE name='Macsmoke' AND online=0 AND map=0 AND zone=12), ',',
 (SELECT COUNT(DISTINCT CreatureID) FROM creature_template_model WHERE CreatureID IN (448,823))
);
""")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("check", "create", "remove"))
    parser.add_argument("--spawn-entry", choices=("id", "id1"), default="id")
    parser.add_argument("--casters", type=int, choices=(12, 24), default=24)
    options = parser.parse_args()
    if options.action == "create":
        create(options.spawn_entry, options.casters)
    else:
        {"check": check, "remove": remove}[options.action](options.spawn_entry)


if __name__ == "__main__":
    main()
