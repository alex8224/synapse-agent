
def create_transparent_glyph_svg():
    # Scale and center the glyph to fill ~410x380 of the 512x512 canvas
    # Original bounds: X [152-32, 360+32] = [120, 392] (width 272)
    #                  Y [140-32, 332+32] = [108, 364] (height 256)
    # Center was (256, 236)
    # Scale factor: 410 / 272 = ~1.5
    # Translation to center at (256, 256):
    # center is (256, 236), so shift Y by +20, then scale around (256, 256)
    
    svg = '''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" width="512" height="512">
  <defs>
    <!-- Dynamic Conduits Gradients -->
    <linearGradient id="topToBottomRightGrad" x1="256" y1="110"
      x2="412" y2="398" gradientUnits="userSpaceOnUse">
      <stop offset="0%" stop-color="#38bdf8" />
      <stop offset="50%" stop-color="#3b82f6" />
      <stop offset="100%" stop-color="#4f46e5" />
    </linearGradient>
    <linearGradient id="bottomGrad" x1="412" y1="398"
      x2="100" y2="398" gradientUnits="userSpaceOnUse">
      <stop offset="0%" stop-color="#4f46e5" />
      <stop offset="50%" stop-color="#2563eb" />
      <stop offset="100%" stop-color="#38bdf8" />
    </linearGradient>
    <linearGradient id="topToBottomLeftGrad" x1="256" y1="110"
      x2="100" y2="398" gradientUnits="userSpaceOnUse">
      <stop offset="0%" stop-color="#38bdf8" />
      <stop offset="60%" stop-color="#2563eb" />
      <stop offset="100%" stop-color="#38bdf8" />
    </linearGradient>
    <linearGradient id="conduitToCenter" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#38bdf8" />
      <stop offset="100%" stop-color="#4f46e5" />
    </linearGradient>

    <!-- Subtle Drop Shadow for Contrast against light & dark backgrounds -->
    <filter id="glyphShadow" x="-20%" y="-20%" width="140%" height="140%">
      <feDropShadow dx="0" dy="8" stdDeviation="12" flood-color="#000000" flood-opacity="0.35" />
      <feDropShadow dx="0" dy="2" stdDeviation="4" flood-color="#000000" flood-opacity="0.25" />
    </filter>
  </defs>

  <!-- PURE TRANSPARENT BACKGROUND (No squircle, no tile) -->

  <!-- Scaled & Centered Tri-Node Synaptic Glyph -->
  <!-- Coordinates:
       Apex: (256, 110)
       Bottom-Right: (412, 398)
       Bottom-Left: (100, 398)
       Center Nexus: (256, 302)
  -->
  <g filter="url(#glyphShadow)">
    <!-- Outer Triangle Conduits (Stroke width 38px, rounded join) -->
    <path d="M 256 110 L 412 398" fill="none" stroke="url(#topToBottomRightGrad)"
      stroke-width="38" stroke-linecap="round" stroke-linejoin="round" />
    <path d="M 412 398 L 100 398" fill="none" stroke="url(#bottomGrad)"
      stroke-width="38" stroke-linecap="round" stroke-linejoin="round" />
    <path d="M 100 398 L 256 110" fill="none" stroke="url(#topToBottomLeftGrad)"
      stroke-width="38" stroke-linecap="round" stroke-linejoin="round" />

    <!-- Interior Axons to Central Nexus (Stroke width 28px) -->
    <path d="M 256 110 L 256 302" fill="none" stroke="url(#conduitToCenter)"
      stroke-width="28" stroke-linecap="round" />
    <path d="M 412 398 L 256 302" fill="none" stroke="url(#conduitToCenter)"
      stroke-width="28" stroke-linecap="round" />
    <path d="M 100 398 L 256 302" fill="none" stroke="url(#conduitToCenter)"
      stroke-width="28" stroke-linecap="round" />

    <!-- Center Junction Nexus Disc -->
    <circle cx="256" cy="302" r="32" fill="#18181b" stroke="#3b82f6" stroke-width="8" />
    <circle cx="256" cy="302" r="14" fill="#ffffff" />

    <!-- Node 1: Apex Node (256, 110) -->
    <g>
      <circle cx="256" cy="110" r="48" fill="#38bdf8" />
      <circle cx="256" cy="110" r="28" fill="#18181b" />
      <circle cx="256" cy="110" r="14" fill="#ffffff" />
    </g>

    <!-- Node 2: Bottom-Right Node (412, 398) -->
    <g>
      <circle cx="412" cy="398" r="48" fill="#4f46e5" />
      <circle cx="412" cy="398" r="28" fill="#18181b" />
      <circle cx="412" cy="398" r="14" fill="#ffffff" />
    </g>

    <!-- Node 3: Bottom-Left Node (100, 398) -->
    <g>
      <circle cx="100" cy="398" r="48" fill="#2563eb" />
      <circle cx="100" cy="398" r="28" fill="#18181b" />
      <circle cx="100" cy="398" r="14" fill="#ffffff" />
    </g>
  </g>
</svg>'''
    
    svg_path = 'rust/synapse-gui/icons/synapse-glyph-transparent.svg'
    with open(svg_path, 'w', encoding='utf-8') as f:
        f.write(svg)
    print('Created transparent glyph SVG at', svg_path)
    return svg_path

if __name__ == '__main__':
    create_transparent_glyph_svg()
