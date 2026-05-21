# Adding Images to the ReClaim Pitch Deck
# ==========================================
#
# The pitch.html has an `.img-slot` CSS class ready for images.
# Here's how to insert an image into any slide:
#
# STEP 1: Place your image file in the same folder as pitch.html
#         (or use a relative/absolute path)
#
# STEP 2: Add this HTML snippet wherever you want an image in a slide:
#
#   <div class="img-slot">
#     <img src="your-image.png" alt="Description of image">
#   </div>
#
# The .img-slot container is styled at 16:9 ratio, max-width 480px,
# with object-fit:cover so images fill the frame cleanly.
#
# EXAMPLE: Adding a screenshot to the title slide (slide-0):
#
#   Find this block in the HTML:
#     <div class="github-tag">
#       <span>Open source</span>
#       <span class="repo">arcsphere/reclaim</span>
#     </div>
#
#   Add BEFORE or AFTER it:
#     <div class="img-slot">
#       <img src="reclaim-screenshot.png" alt="ReClaim interface screenshot">
#     </div>
#
# EXAMPLE: Adding a diagram to the competitive landscape slide (slide-3):
#
#   Add after the .source-strip div:
#     <div class="img-slot" style="max-width:640px; margin-top:24px;">
#       <img src="reclaim-architecture.png" alt="ReClaim architecture diagram">
#     </div>
#
# CUSTOMIZING SIZE:
#   - Change max-width:  style="max-width:320px"  (smaller)
#   - Change max-width:  style="max-width:100%"   (full width)
#   - Change aspect ratio: style="aspect-ratio:4/3" (taller)
#   - Remove aspect-ratio for auto-height: style="aspect-ratio:unset"
#
# FULL-BLEED BACKGROUND IMAGE ON A SLIDE:
#   Add to any .slide element:
#     style="background:url('hero.jpg') center/cover no-repeat"
#   Then add a dark overlay for text readability:
#     <div style="position:absolute;inset:0;background:rgba(0,0,0,0.7);z-index:0"></div>
#   And wrap slide content in:
#     <div style="position:relative;z-index:1"> ... </div>
