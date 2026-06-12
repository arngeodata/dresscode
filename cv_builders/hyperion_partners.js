/**
 * Hyperion Partners — CV builder
 *
 * Reads:  <cv_json_path>     — ParsedCV as JSON (from Claude parsing)
 *         <header_img_path>  — branded header PNG (empty string if none)
 * Writes: <output_path>      — formatted .docx
 *
 * Called by the Dresscode worker:
 *   node cv_builder.js <cv_json_path> <header_img_path> <output_path>
 *
 * Style rules (from Hyperion Partners CV Style Guide):
 *   - Calibri 11pt throughout, all black, no colours or borders
 *   - NoSpacing (0pt before/after) — blank paragraphs create visual gaps
 *   - Document title: "Firstname Lastname – CV" (en dash), bold, centred
 *   - Section headings: bold, left-aligned, preceded by one blank line
 *   - Profile: first-person prose, single paragraph, regular weight
 *   - Education: bullet list, one entry per bullet, reverse-chron
 *   - Skills: bullet list, one skill phrase per bullet
 *   - Career History: Date → Company → Job Title (all bold, separate lines),
 *     then bullet points (regular weight)
 *   - No contact details (phone, email, address)
 *   - Header image flush to top of page (margin.header: 0)
 */

'use strict';

const {
  Document, Packer, Paragraph, TextRun, Header,
  AlignmentType, LevelFormat, ImageRun
} = require('docx');
const fs = require('fs');

// ── CLI args ──────────────────────────────────────────────────────────────────
const [cvPath, headerImgPath, outputPath] = process.argv.slice(2);

if (!cvPath || !outputPath) {
  console.error('Usage: node cv_builder.js <cv_json_path> <header_img_path_or_empty> <output_path>');
  process.exit(1);
}

// ── Load inputs ───────────────────────────────────────────────────────────────
const cv = JSON.parse(fs.readFileSync(cvPath, 'utf8'));
const headerImage = headerImgPath && fs.existsSync(headerImgPath)
  ? fs.readFileSync(headerImgPath)
  : null;

// ── Style constants ───────────────────────────────────────────────────────────
const FONT      = 'Calibri';
const SIZE      = 22;          // docx uses half-points; 22 = 11pt
const NS        = { before: 0, after: 0 };  // NoSpacing

// ── Helpers ───────────────────────────────────────────────────────────────────

/** Bullet list numbering config */
function makeBulletConfig(ref) {
  return {
    reference: ref,
    levels: [{
      level: 0,
      format: LevelFormat.BULLET,
      text: '•',
      alignment: AlignmentType.LEFT,
      style: { paragraph: { indent: { left: 720, hanging: 360 } } }
    }]
  };
}

/** Bold paragraph */
function B(text, alignment) {
  return new Paragraph({
    spacing: NS,
    alignment: alignment || AlignmentType.LEFT,
    children: [new TextRun({ text: text || '', bold: true, font: FONT, size: SIZE })]
  });
}

/** Regular-weight paragraph */
function R(text) {
  return new Paragraph({
    spacing: NS,
    children: [new TextRun({ text: text || '', font: FONT, size: SIZE })]
  });
}

/** Empty separator line */
function blank() {
  return new Paragraph({
    spacing: NS,
    children: [new TextRun({ text: '', font: FONT, size: SIZE })]
  });
}

/** Bullet point */
function bullet(text, ref) {
  return new Paragraph({
    numbering: { reference: ref, level: 0 },
    spacing: NS,
    children: [new TextRun({ text: (text || '').trim(), font: FONT, size: SIZE })]
  });
}

/** Strip leading bullet/dash characters Claude sometimes includes */
function cleanBullet(text) {
  return (text || '').replace(/^[\-\•\*]\s*/, '').trim();
}

/** Never render an ALL-CAPS heading: if the title has no lowercase letters,
 *  convert it to Title Case; otherwise keep the candidate's original casing. */
function normalizeHeading(text) {
  const t = (text || '').trim();
  if (!t || /[a-z]/.test(t)) return t;
  return t.replace(/[A-Za-z']+/g, w => w.charAt(0).toUpperCase() + w.slice(1).toLowerCase());
}

// ── Document content ──────────────────────────────────────────────────────────
const rawName  = cv.candidate?.full_name || 'Candidate';
// Normalise to title case so ALL-CAPS source CVs render correctly
// Use [A-Za-z']+ so hyphens are preserved and each part is title-cased independently
const name     = rawName.replace(/[A-Za-z']+/g, w => w.charAt(0).toUpperCase() + w.slice(1).toLowerCase());
const children = [];

// One blank line separates the header image from the document title
children.push(blank());

// Document title: "Firstname Lastname – CV" (U+2013 en dash)
children.push(new Paragraph({
  spacing: NS,
  alignment: AlignmentType.CENTER,
  children: [new TextRun({
    text: `${name} – CV`,
    bold: true,
    font: FONT,
    size: SIZE
  })]
}));

// ── Profile ───────────────────────────────────────────────────────────────────
if (cv.summary) {
  children.push(blank());
  children.push(B('Profile'));
  children.push(R(cv.summary));
}

// ── Education ─────────────────────────────────────────────────────────────────
if (cv.education && cv.education.length > 0) {
  children.push(blank());
  children.push(B('Education'));

  for (const edu of cv.education) {
    // Build: "Qualification – [Grade – ]Institution[, Year]"
    const parts = [];
    if (edu.qualification) parts.push(edu.qualification);
    if (edu.grade)         parts.push(edu.grade);
    if (edu.institution)   parts.push(edu.institution);

    let line = parts.join(' – ');
    if (edu.year) line += `, ${edu.year}`;

    children.push(bullet(line, 'bullets-edu'));
  }
}

// ── Skills ────────────────────────────────────────────────────────────────────
if (cv.skills && cv.skills.length > 0) {
  children.push(blank());
  children.push(B('Skills'));

  for (const skill of cv.skills) {
    children.push(bullet(skill, 'bullets-skills'));
  }
}

// ── Career History ────────────────────────────────────────────────────────────
if (cv.experience && cv.experience.length > 0) {
  children.push(blank());
  children.push(B('Career History'));

  cv.experience.forEach((job, idx) => {
    const bulletRef = `bullets-r${idx + 1}`;

    // Blank line between roles (not before the first one)
    if (idx > 0) children.push(blank());

    // Date line: "Month YYYY – Month YYYY" or "Month YYYY – Present"
    const dateParts = [];
    if (job.start_date) dateParts.push(job.start_date);
    if (job.end_date)        dateParts.push(job.end_date);
    else if (job.start_date) dateParts.push('Present');
    const dateLine = dateParts.join(' – ');

    // Three bold header lines: date, company, title
    if (dateLine)    children.push(B(dateLine));
    if (job.company) children.push(B(job.company));
    if (job.title)   children.push(B(job.title));

    // Bullet points
    for (const resp of (job.responsibilities || [])) {
      const text = cleanBullet(resp);
      if (text) children.push(bullet(text, bulletRef));
    }
  });
}

// ── Extra sections (catch-all for anything outside core sections) ─────────────
// Reference/referee sections are suppressed — Hyperion does not include them
const SUPPRESS_SECTIONS = ['reference', 'referee'];
if (cv.extra_sections && cv.extra_sections.length > 0) {
  cv.extra_sections.forEach((section, idx) => {
    if (!section.title) return;
    if (SUPPRESS_SECTIONS.some(s => section.title.toLowerCase().includes(s))) return;
    const bulletRef = `bullets-extra-${idx}`;
    children.push(blank());
    children.push(B(normalizeHeading(section.title)));
    for (const item of (section.items || [])) {
      const text = cleanBullet(item);
      if (text) children.push(bullet(text, bulletRef));
    }
  });
}

// ── Bullet numbering configs (edu, skills, up to 20 roles, up to 10 extra) ───
const bulletConfigs = [
  makeBulletConfig('bullets-edu'),
  makeBulletConfig('bullets-skills'),
  ...Array.from({ length: 20 }, (_, i) => makeBulletConfig(`bullets-r${i + 1}`)),
  ...Array.from({ length: 10 }, (_, i) => makeBulletConfig(`bullets-extra-${i}`))
];

// ── Header section (branded image or empty) ───────────────────────────────────
const sectionHeaders = headerImage
  ? {
      default: new Header({
        children: [
          new Paragraph({
            spacing: NS,
            indent: { left: -1418 },  // extends image past left margin to page edge
            children: [
              new ImageRun({
                type: 'png',
                data: headerImage,
                transformation: { width: 829, height: 207 },
                altText: { title: 'Header', description: 'Company header image', name: 'Header' }
              })
            ]
          })
        ]
      })
    }
  : {};

// ── Assemble document ─────────────────────────────────────────────────────────
const doc = new Document({
  numbering: { config: bulletConfigs },
  styles: {
    default: { document: { run: { font: FONT, size: SIZE } } }
  },
  sections: [{
    properties: {
      page: {
        size: { width: 11906, height: 16838 },   // A4 in twentieths of a point
        margin: {
          top: 1440, right: 1440, bottom: 1440, left: 1440,
          header: 0,    // flush header image to top of page
          footer: 708
        }
      }
    },
    headers: sectionHeaders,
    children
  }]
});

Packer.toBuffer(doc)
  .then(buf => {
    fs.writeFileSync(outputPath, buf);
    console.log('Done:', outputPath);
  })
  .catch(err => {
    console.error('Builder error:', err);
    process.exit(1);
  });
