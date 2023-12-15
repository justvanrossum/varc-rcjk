from font import *
from rcjkTools import *
from flatFont import buildFlatGlyph
from component import *

from fontTools.ttLib import newTable
from fontTools.ttLib.tables._g_l_y_f import Glyph, GlyphCoordinates
from fontTools.varLib.models import normalizeLocation, VariationModel
from fontTools.varLib.varStore import OnlineVarStoreBuilder
import fontTools.ttLib.tables.otTables as ot
from functools import partial
from collections import defaultdict
import struct


async def closureGlyph(rcjkfont, glyphs, glyph):
    assert glyph.sources[0].name == "<default>"
    assert glyph.sources[0].layerName == "foreground"
    layer = glyph.layers["foreground"]
    for component in layer.glyph.components:
        if component.name not in glyphs:
            componentGlyph = await rcjkfont.getGlyph(component.name)
            glyphs[component.name] = componentGlyph
            await closureGlyph(rcjkfont, glyphs, componentGlyph)


async def closureGlyphs(rcjkfont, glyphs):
    for glyph in list(glyphs.values()):
        await closureGlyph(rcjkfont, glyphs, glyph)


def setupFvarAxes(rcjkfont, glyphs):
    fvarAxes = []
    for axis in rcjkfont.designspace["axes"]:
        fvarAxes.append(
            (
                axis["tag"],
                axis["minValue"],
                axis["defaultValue"],
                axis["maxValue"],
                axis["name"],
            )
        )

    maxAxes = 0
    for glyph in glyphs.values():
        axes = {
            axis.name: (axis.minValue, axis.defaultValue, axis.maxValue)
            for axis in glyph.axes
        }
        maxAxes = max(maxAxes, len(axes))

    for i in range(maxAxes):
        tag = "%04d" % i
        fvarAxes.append((tag, -1, 0, 1, tag))

    return fvarAxes


async def buildVarcFont(rcjkfont, glyphs):
    print("Building varc.ttf")

    await closureGlyphs(rcjkfont, glyphs)

    publicAxes = set()
    for axis in rcjkfont.designspace["axes"]:
        publicAxes.add(axis["tag"])
    fvarAxes = setupFvarAxes(rcjkfont, glyphs)
    fvarTags = [axis[0] for axis in fvarAxes]

    fb = await createFontBuilder(rcjkfont, "rcjk", "varc", glyphs, glyphDataFormat=1)
    reverseGlyphMap = fb.font.getReverseGlyphMap()

    fbGlyphs = {".notdef": Glyph()}
    fbVariations = {}
    varcGlyphs = {}
    varIdxMap = ot.DeltaSetIndexMap()
    varIdxMapping = varIdxMap.mapping = []
    varIdxMappingMap = {}
    varStoreBuilder = OnlineVarStoreBuilder(fvarTags)

    for glyph in glyphs.values():
        glyph_masters = glyphMasters(glyph)

        axes = {
            axis.name: (axis.minValue, axis.defaultValue, axis.maxValue)
            for axis in glyph.axes
        }
        axesMap = {}
        for i, name in enumerate(axes.keys()):
            axesMap[name] = "%04d" % i if name not in fvarTags else name

        if glyph_masters[()].glyph.path.coordinates:
            # Simple glyph...

            fbGlyphs[glyph.name], fbVariations[glyph.name] = await buildFlatGlyph(
                rcjkfont, glyph, axesMap
            )
            continue

        # VarComposite glyph...

        fbGlyphs[glyph.name] = Glyph()

        componentAnalysis = analyzeComponents(glyph_masters, glyphs, axes, publicAxes)

        glyphRecord = varcGlyphs[glyph.name] = ot.VarCompositeGlyph()
        glyphRecord.populateDefaults()
        componentRecords = glyphRecord.components

        layer = next(iter(glyph_masters.values()))  # Default master
        for ci, component in enumerate(layer.glyph.components):
            rec = buildComponentRecord(
                component,
                glyphs[component.name],
                componentAnalysis[ci],
                fvarAxes,
            )
            componentRecords.append(rec)

        #
        # Build variations
        #

        masterLocs = list(dictifyLocation(l) for l in glyph_masters.keys())
        masterLocs = [normalizeLocation(m, axes) for m in masterLocs]
        masterLocs = [{axesMap[k]: v for k, v in loc.items()} for loc in masterLocs]

        model = VariationModel(masterLocs, list(axes.keys()))
        varStoreBuilder.setModel(model)

        for ci, rec in enumerate(componentRecords):
            allCoordinateMasters = []
            allTransformMasters = []
            for loc, layer in glyph_masters.items():
                component = layer.glyph.components[ci]

                coordinateMasters, transformMasters = getComponentMasters(
                    rcjkfont, component, glyphs[component.name], componentAnalysis[ci]
                )
                allCoordinateMasters.append(coordinateMasters)
                allTransformMasters.append(transformMasters)

            if rec.flags & (VarComponentFlags.AXIS_VALUES_HAVE_VARIATION | VarComponentFlags.TRANSFORM_HAS_VARIATION):
                baseIdx = len(varIdxMapping)
                vec = []

                if rec.flags & VarComponentFlags.AXIS_VALUES_HAVE_VARIATION:
                    for masterValues in zip(*allCoordinateMasters):
                        base, varIdx = varStoreBuilder.storeMasters(masterValues)
                        assert base == masterValues[0]
                        vec.append(varIdx)

                if rec.flags & VarComponentFlags.TRANSFORM_HAS_VARIATION:
                    for masterValues in zip(*allTransformMasters):
                        base, varIdx = varStoreBuilder.storeMasters(masterValues)
                        assert base == masterValues[0]
                        vec.append(varIdx)

                existingBase = varIdxMappingMap.get(tuple(vec))
                if existingBase is not None:
                    rec.varIndexBase = existingBase
                else:
                    rec.varIndexBase = baseIdx
                    varIdxMapping.extend(vec)
                    varIdxMappingMap[tuple(vec)] = baseIdx

    varStore = varStoreBuilder.finish()
    mapping = varStore.optimize(use_NO_VARIATION_INDEX=False)
    varIdxMapping = [mapping[i] for i in varIdxMapping]

    varc = newTable("VARC")
    varcTable = varc.table = ot.VARC()
    varcTable.Version = 0x00010000

    coverage = varcTable.Coverage = ot.Coverage()
    coverage.glyphs = [glyph for glyph in varcGlyphs.keys()]

    varCompositeGlyphs = varcTable.VarCompositeGlyphs = ot.VarCompositeGlyphs()
    varCompositeGlyphs.glyphs = list(varcGlyphs.values())

    varcTable.VarIndexMap = varIdxMap
    varcTable.VarStore = varStore

    fb.setupFvar(fvarAxes, [])
    fb.setupGlyf(fbGlyphs, validateGlyphFormat=False)
    fb.setupGvar(fbVariations)
    recalcSimpleGlyphBounds(fb)
    fixLsb(fb)
    fb.font.recalcBBoxes = False
    fb.font["VARC"] = varc
    fb.save("varc.ttf")
