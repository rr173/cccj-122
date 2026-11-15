"""Build the demonstration taxonomy package ZIP from in-memory XML.

The package is deliberately tiny but exercises every required behaviour:

* two ``rewriteURI`` prefixes where one is a strict prefix of the other, so a
  long public URI must take the longest match while a short URI falls back;
* an ``xs:import`` reaching a type schema on the public namespace;
* an ``xs:include`` written relative to an ``xml:base`` directory;
* two entry schemas sharing one imported schema;
* an XBRL ``linkbaseRef`` inside an entry schema;
* a dependency cycle between two schemas;
* many unrelated ZIP members that must never be parsed as schemas.
"""
from __future__ import annotations

import os
import stat
import zipfile

TOP = "demo-taxonomy"

# Public namespaces used inside the schemas.
NS_BASE = "http://example.com/taxonomy/base"
NS_LONG = "http://example.com/taxonomy/base/extensions/2025"
NS_CYCLE = "http://example.com/taxonomy/cycle"
XLINK_NS = "http://www.w3.org/1999/xlink"
LINK_NS = "http://www.xbrl.org/2003/linkbase"
TP_NS = "http://xbrl.org/2016/taxonomy-package"
CAT_NS = "urn:oasis:names:tc:entity:xmlns:xml:catalog"

DESCRIPTOR = f"""<?xml version="1.0" encoding="utf-8"?>
<tp:taxonomyPackage xmlns:tp="{TP_NS}">
  <tp:identifier>com.example.demo-taxonomy</tp:identifier>
  <tp:name>Demo Taxonomy</tp:name>
  <tp:version>1.0.0</tp:version>
  <tp:entryPoints>
    <tp:entryPoint>
      <tp:entryPointDocument href="schemas/ep1.xsd"/>
    </tp:entryPoint>
    <tp:entryPoint>
      <tp:entryPointDocument href="schemas/ep2.xsd"/>
    </tp:entryPoint>
  </tp:entryPoints>
</tp:taxonomyPackage>
"""

# Two overlapping prefixes:
#   long  .../base/extensions/2025 -> schemas/ext/
#   short .../base                 -> schemas/base/
CATALOG = f"""<?xml version="1.0" encoding="utf-8"?>
<catalog xmlns="{CAT_NS}" prefer="public">
  <rewriteURI uriStartString="{NS_LONG}"
              rewritePrefix="xbrl-pkg://package/schemas/ext/"/>
  <rewriteURI uriStartString="{NS_BASE}"
              rewritePrefix="xbrl-pkg://package/schemas/base/"/>
  <rewriteURI uriStartString="{NS_CYCLE}"
              rewritePrefix="xbrl-pkg://package/schemas/cycle/"/>
</catalog>
"""

EP1 = f"""<?xml version="1.0" encoding="utf-8"?>
<schema xmlns="http://www.w3.org/2001/XMLSchema"
        xmlns:link="{LINK_NS}" xmlns:xlink="{XLINK_NS}"
        targetNamespace="http://example.com/taxonomy/ep1"
        elementFormDefault="qualified">
  <annotation>
    <appinfo>
      <link:linkbaseRef xlink:type="simple"
                        xlink:href="../linkbases/labels.xml"
                        xlink:arcrole="http://www.xbrl.org/2003/arcrole/concept-label"
                        xlink:role="http://www.xbrl.org/2003/role/labelLinkbaseRef"/>
    </appinfo>
  </annotation>
  <import namespace="{NS_BASE}" schemaLocation="{NS_BASE}/types.xsd"/>
  <import namespace="{NS_LONG}" schemaLocation="{NS_LONG}/ext-types.xsd"/>
</schema>
"""

EP2 = f"""<?xml version="1.0" encoding="utf-8"?>
<schema xmlns="http://www.w3.org/2001/XMLSchema"
        targetNamespace="http://example.com/taxonomy/ep2"
        elementFormDefault="qualified">
  <import namespace="{NS_BASE}" schemaLocation="{NS_BASE}/types.xsd"/>
  <import namespace="{NS_CYCLE}" schemaLocation="{NS_CYCLE}/cyclic-a.xsd"/>
</schema>
"""

# types.xsd includes types-base.xsd through an xml:base directory: the
# include's href is relative to xml:base, not to the schema's own folder.
TYPES = f"""<?xml version="1.0" encoding="utf-8"?>
<schema xmlns="http://www.w3.org/2001/XMLSchema"
        targetNamespace="{NS_BASE}"
        elementFormDefault="qualified"
        xml:base="parts/">
  <include schemaLocation="types-base.xsd"/>
</schema>
"""

TYPES_BASE = f"""<?xml version="1.0" encoding="utf-8"?>
<schema xmlns="http://www.w3.org/2001/XMLSchema"
        targetNamespace="{NS_BASE}"
        elementFormDefault="qualified">
  <simpleType name="DemoTextType">
    <restriction base="string"/>
  </simpleType>
</schema>
"""

# Reached only through the LONG prefix; also includes types-base via a
# public import to demonstrate prefix-independent resolution.
EXT_TYPES = f"""<?xml version="1.0" encoding="utf-8"?>
<schema xmlns="http://www.w3.org/2001/XMLSchema"
        targetNamespace="{NS_LONG}"
        elementFormDefault="qualified">
  <import namespace="{NS_BASE}" schemaLocation="{NS_BASE}/types.xsd"/>
  <simpleType name="DemoExtendedType">
    <restriction base="string"/>
  </simpleType>
</schema>
"""

# A two-node dependency cycle. Both files stay in the closure once; the edge
# back to an in-progress node is recorded as a cycle edge.
CYCLIC_A = f"""<?xml version="1.0" encoding="utf-8"?>
<schema xmlns="http://www.w3.org/2001/XMLSchema"
        targetNamespace="{NS_CYCLE}"
        elementFormDefault="qualified">
  <include schemaLocation="{NS_CYCLE}/cyclic-b.xsd"/>
  <complexType name="CyclicA"/>
</schema>
"""

CYCLIC_B = f"""<?xml version="1.0" encoding="utf-8"?>
<schema xmlns="http://www.w3.org/2001/XMLSchema"
        targetNamespace="{NS_CYCLE}"
        elementFormDefault="qualified">
  <include schemaLocation="{NS_CYCLE}/cyclic-a.xsd"/>
  <complexType name="CyclicB"/>
</schema>
"""

LABELS = f"""<?xml version="1.0" encoding="utf-8"?>
<linkbase xmlns="{LINK_NS}" xmlns:xlink="{XLINK_NS}">
  <labelLink xlink:type="simple" xlink:role="http://www.xbrl.org/2003/role/link">
    <loc xlink:type="locator" xlink:href="../schemas/ep1.xsd#DemoConcept"/>
    <label xlink:label="lbl" xlink:role="http://www.xbrl.org/2003/role/label"
           xml:lang="en">Demo concept</label>
  </labelLink>
</linkbase>
"""

MEMBERS = {
    "META-INF/taxonomyPackage.xml": DESCRIPTOR,
    "META-INF/catalog.xml": CATALOG,
    "schemas/ep1.xsd": EP1,
    "schemas/ep2.xsd": EP2,
    "schemas/base/types.xsd": TYPES,
    "schemas/base/parts/types-base.xsd": TYPES_BASE,
    "schemas/ext/ext-types.xsd": EXT_TYPES,
    "schemas/cycle/cyclic-a.xsd": CYCLIC_A,
    "schemas/cycle/cyclic-b.xsd": CYCLIC_B,
    "linkbases/labels.xml": LABELS,
}


def build_zip(path: str, *, junk_count: int = 200,
              extra_members: dict | None = None,
              symlink_member: str | None = None) -> str:
    """Write the demonstration ZIP; returns the path."""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, text in MEMBERS.items():
            archive.writestr(f"{TOP}/{name}", text)
        # Lots of unrelated members: data files and XML outside the closure.
        for i in range(junk_count):
            archive.writestr(f"{TOP}/junk/notes-{i:04d}.txt",
                             f"unrelated note {i}\n")
        for i in range(20):
            archive.writestr(
                f"{TOP}/junk/stray-{i:02d}.xsd",
                '<?xml version="1.0"?><schema xmlns="http://www.w3.org/2001/XMLSchema"/>')
        if extra_members:
            for name, data in extra_members.items():
                archive.writestr(f"{TOP}/{name}", data)
        if symlink_member:
            info = zipfile.ZipInfo(f"{TOP}/{symlink_member}")
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, "ep1.xsd")
    return path


def package_member_names() -> list[str]:
    return sorted(MEMBERS)
