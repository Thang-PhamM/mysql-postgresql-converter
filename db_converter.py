#!/usr/bin/env python3

"""
Fixes a MySQL dump made with the right format so it can be directly
imported to a new PostgreSQL database.

Dump using:
mysqldump --compatible=postgresql --default-character-set=utf8 -r databasename.mysql -u root databasename
"""

import re
import sys
import os
import time
import subprocess


def parse(input_filename, *output_filename):
    """Feed it a file, and it'll output a fixed one"""

    # State storage
    if input_filename == "-":
        num_lines = -1
    else:
        num_lines = int(subprocess.check_output(["wc", "-l", input_filename]).strip().split()[0])
    tables = {}
    current_table = None
    creation_lines = []
    enum_types = []
    foreign_key_lines = []
    fulltext_key_lines = []
    index_lines = []
    sequence_lines = []
    cast_lines = []
    comment_lines = []
    num_inserts = 0
    started = time.time()

    # Open files
    if len(output_filename) == 1:
        output_filename = output_filename[0]
        schema_f= data_f= constraint_f = open(output_filename, "w", encoding="utf-8")
    else:
        schema_f = open(output_filename[0], "w", encoding="utf-8")
        data_f = open(output_filename[1], "w", encoding="utf-8")
        constraint_f = open(output_filename[2], "w", encoding="utf-8")
    # output = open(output_filename, "w", encoding="utf-8")
    logging = sys.stdout

    if input_filename == "-":
        input_fh = sys.stdin
    else:
        input_fh = open(input_filename, encoding="utf-8")

    schema_f.write("START TRANSACTION;\n")
    schema_f.write("SET standard_conforming_strings=off;\n")
    schema_f.write("SET escape_string_warning=off;\n")
    schema_f.write("SET CONSTRAINTS ALL DEFERRED;\n\n")

    insert = False
    for i, line in enumerate(input_fh):
        time_taken = time.time() - started
        percentage_done = (i + 1) / float(num_lines)
        secs_left = (time_taken / percentage_done) - time_taken
        # logging.write(f"\rLine {i + 1} (of {num_lines}: {percentage_done * 100:.2f}%) [{len(tables)} tables] [{num_inserts} inserts] [ETA: {int(secs_left // 60)} min {int(secs_left % 60)} sec]")
        # logging.flush()
        line = line.strip().replace(r"\\", "WUBWUBREALSLASHWUB").replace(r"\'", "''").replace("WUBWUBREALSLASHWUB", r"\\")
        # Ignore comment lines
        if line.startswith("--") or line.startswith("/*") or line.startswith("LOCK TABLES") or line.startswith("DROP TABLE") or line.startswith("UNLOCK TABLES") or not line:
            continue
    
        line = line.replace('`', '"')

        # Outside of anything handling
        if current_table is None:
            # Start of a table creation statement?
            if line.startswith("CREATE TABLE"):
                current_table = line.split('"')[1]
                tables[current_table] = {"columns": []}
                creation_lines = []
            # Inserting data into a table?
            elif line.startswith("INSERT INTO") or insert:
                insert = True
                data_f.write(line.replace("'0000-00-00 00:00:00'", "NULL") + "\n")
                num_inserts += 1
                if line.endswith(");"):
                    insert = False
            elif line.startswith("DELETE FROM"):
                pass
            # ???
            else:
                print(f"\n ! Unknown line in main body: {line}")

        # Inside-create-statement handling
        else:
            # Is it a column?
            if line.startswith('"'):
                useless, name, definition = line.strip(",").split('"', 2)
                try:
                    type, extra = definition.strip().split(" ", 1)

                    # This must be a tricky enum
                    if ')' in extra:
                        type, extra = definition.strip().split(")")

                except ValueError:
                    type = definition.strip()
                    extra = ""
                extra = re.sub(r"CHARACTER SET [\w\d]+\s*", "", extra.replace("unsigned", ""))
                extra = re.sub(r"COLLATE [\w\d]+\s*", "", extra.replace("unsigned", ""))

                # Extract comment for PostgreSQL
                comment_match = re.search(r"COMMENT\s+'([^']*)'", extra, re.IGNORECASE)
                comment = comment_match.group(1) if comment_match else None
                extra = re.sub(r"COMMENT\s+'[^']*'", "", extra, flags=re.IGNORECASE).strip()

                auto_increment = extra.find("AUTO_INCREMENT") != -1
                extra = extra.replace("AUTO_INCREMENT", "").strip()
                
                # Handle NOT NULL constraint - only add if not already specified
                if "NOT NULL" not in extra and "NULL" not in extra and final_type != "boolean":
                    # Add NOT NULL only if neither NULL nor NOT NULL is specified
                    extra = f"{extra} NOT NULL".strip() if extra else "NOT NULL"

                # See if it needs type conversion
                final_type = None
                if type.startswith("tinyint(") or type.startswith("smallint("):
                    if type.startswith("tinyint(1)"):
                        final_type = "boolean"
                    if auto_increment:
                        type = "serial"
                    else:
                        type = "smallint"
                elif type.startswith("int(") or type.startswith("mediumint("):
                    type = "integer"
                    if auto_increment:
                        type = "serial"
                elif type.startswith("bigint("):
                    type = "bigint"
                    if auto_increment:
                        type = "bigserial"
                elif type == "longtext" or type.startswith("longtext"):
                    type = "text"
                elif type == "mediumtext" or type.startswith("mediumtext"):
                    type = "text"
                elif type == "tinytext" or type.startswith("tinytext"):
                    type = "text"
                elif type.startswith("varchar("):
                    size = int(type.split("(")[1].rstrip(")"))
                    size = 256 if size == 255 else size
                    type = f"varchar({size})"
                elif type.startswith("datetime") or type.startswith("timestamp"):
                    type = "timestamp with time zone"
                    extra = extra.replace("'0000-00-00 00:00:00'", "NULL")
                    extra = extra.replace("on update current_timestamp()", "")
                    extra = extra.replace("current_timestamp()", "CURRENT_TIMESTAMP")
                    extra = extra.replace("CURRENT_TIMESTAMP()", "CURRENT_TIMESTAMP")
                elif type.startswith("double") or type.startswith("float"):
                    type = "double precision"
                elif type.endswith("blob"):
                    type = "bytea"
                elif type.startswith("enum(") or type.startswith("set("):
                    types_str = type.split("(")[1].rstrip(")").rstrip('"')
                    types_arr = [type_str.strip('\'') for type_str in types_str.split(",")]

                    # Considered using values to make a name, but it's dodgy
                    # enum_name = '_'.join(types_arr)
                    enum_name = f"{current_table}_{name}"

                    if enum_name not in enum_types:
                        schema_f.write(f"CREATE TYPE {enum_name} AS ENUM ({types_str});\n")
                        enum_types.append(enum_name)

                    type = enum_name
                elif type.startswith("json"):
                    type = "jsonb"  # or "json" depending on your needs
                elif type.startswith("decimal(") or type.startswith("numeric("):
                    # Keep the precision and scale
                    type = type.replace("decimal", "numeric")

                if final_type:
                    cast_lines.append(f'ALTER TABLE "{current_table}" ALTER COLUMN "{name}" DROP DEFAULT, ALTER COLUMN "{name}" TYPE {final_type} USING CAST("{name}" as {final_type})')
                
                # Add column comment if exists
                if comment:
                    comment_lines.append(f'COMMENT ON COLUMN "{current_table}"."{name}" IS \'{comment}\';')
                
                # ID fields need sequences [if they are integers?]
                # if name == "id" and set_sequence is True:
                #     sequence_lines.append(f"CREATE SEQUENCE {current_table}_id_seq")
                #     sequence_lines.append(f"SELECT setval('{current_table}_id_seq', max(id)) FROM {current_table}")
                #     sequence_lines.append(f'ALTER TABLE "{current_table}" ALTER COLUMN "id" SET DEFAULT nextval(\'{current_table}_id_seq\')')
                # Record it
                creation_lines.append(f'"{name}" {type} {extra}')
                tables[current_table]['columns'].append((name, type, extra))
            # Is it a constraint or something?
            elif line.startswith("PRIMARY KEY"):
                creation_lines.append(line.rstrip(","))
            elif line.startswith("CONSTRAINT"):
                constraint_def = line.split("CONSTRAINT")[1].strip().rstrip(",")
                foreign_key_lines.append(f'ALTER TABLE "{current_table}" ADD CONSTRAINT {constraint_def} DEFERRABLE INITIALLY DEFERRED')
                # foreign_key_lines.append(f'CREATE INDEX ON "{current_table}" {line.split("FOREIGN KEY")[1].split("REFERENCES")[0].strip().rstrip(",")}')
            elif line.startswith("UNIQUE KEY"):
                creation_lines.append(f"UNIQUE ({line.split('(')[1].split(')')[0]})")
            elif line.startswith("FULLTEXT KEY"):
                fulltext_keys = " || ' ' || ".join(line.split('(')[-1].split(')')[0].replace('"', '').split(','))
                fulltext_key_lines.append(f"CREATE INDEX ON {current_table} USING gin(to_tsvector('english', {fulltext_keys}))")
            elif line.startswith("KEY"):
                index_lines.append(f"CREATE INDEX ON \"{current_table}\" ({line.split('(')[1].split(')')[0]})")
            # Is it the end of the table?
            elif re.match(r"^\s*\).*", line):
            # elif line.startswith(")") and line.endswith(";\n"):
                schema_f.write(f'CREATE TABLE "{current_table}" (\n')
                for i, line in enumerate(creation_lines):
                    schema_f.write(f"    {line}{',' if i != (len(creation_lines) - 1) else ''}\n")
                schema_f.write(');\n\n')
                current_table = None
            # ???
            else:
                print(f"\n ! Unknown line inside table creation: {line}")

    # Finish file
    schema_f.write("\n-- Post-data save --\n")
    schema_f.write("COMMIT;\n")

    # Write typecasts out
    constraint_f.write("\n-- Typecasts --\n")
    for line in cast_lines:
        constraint_f.write(f"{line};\n")
    
    # Write indexes out
    constraint_f.write("\n-- Indexes --\n")
    constraint_f.writelines(f"{line};\n" for line in index_lines)

    # Write FK constraints out
    constraint_f.write("\n-- Foreign keys --\n")
    for line in foreign_key_lines:
        constraint_f.write(f"{line};\n")

    # Write sequences out
    constraint_f.write("\n-- Sequences --\n")
    for line in sequence_lines:
        constraint_f.write(f"{line};\n")

    # Write full-text indexkeyses out
    constraint_f.write("\n-- Full Text keys --\n")
    for line in fulltext_key_lines:
        constraint_f.write(f"{line};\n")

    # Write column comments out
    constraint_f.write("\n-- Column comments --\n")
    for line in comment_lines:
        constraint_f.write(f"{line}\n")

    # Finish file
    constraint_f.write("\n")

    input_fh.close()
    # Close files
    if not schema_f.closed:
        schema_f.close()
    if not data_f.closed:
        data_f.close()
    if not constraint_f.closed:
        constraint_f.close()
    
    print("")

    logging.write(f"File {input_filename} converted in {time.time() - started:.2f} seconds, {len(tables)} tables created, {num_inserts} inserts")
    logging.close()


if __name__ == "__main__":
    # Usage:
    # python org_converter.py inputfile outputfile
    # or
    # python org_converter.py inputfile schemafile datafile constraintsfile
    if len(sys.argv) == 3:
        parse(sys.argv[1], sys.argv[2])
    elif len(sys.argv) == 5:
        parse(sys.argv[1], *sys.argv[2:])
    else:
        print("Usage: org_converter.py inputfile outputfile")
        print("       org_converter.py inputfile schemafile datafile constraintsfile")
        sys.exit(1)

