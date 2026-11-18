/**
 * The "CLIO doing science" carousel on the home page: one capture per field,
 * each from a CLIO session, with what CLIO did and where to read more.
 *
 * Images are processed at build time into responsive WebP. A slide with a dark
 * capture swaps it in with the site theme; one captured in a single theme keeps
 * that image in both.
 */
import { getImage } from 'astro:assets';
import stationsLight from '@/assets/carousel/earth-science/stations-light.webp';
import stationsDark from '@/assets/carousel/earth-science/stations-dark.webp';
import heleneLight from '@/assets/carousel/earth-science/helene-light.webp';
import heleneDark from '@/assets/carousel/earth-science/helene-dark.webp';
import sectionLight from '@/assets/carousel/oceanography/section-light.webp';
import sectionDark from '@/assets/carousel/oceanography/section-dark.webp';
import paraviewLight from '@/assets/carousel/visualization/paraview-light.webp';
import paraviewDark from '@/assets/carousel/visualization/paraview-dark.webp';
import measureLight from '@/assets/carousel/cad/measure-light.webp';
import measureDark from '@/assets/carousel/cad/measure-dark.webp';
import slicerLight from '@/assets/carousel/3d-printing/slicer-light.webp';
import slicerDark from '@/assets/carousel/3d-printing/slicer-dark.webp';
import routedBoard from '@/assets/carousel/electronics/kicad-routed-board.webp';
import boardCase from '@/assets/carousel/electronics/kicad-case.webp';
import hybridTraining from '@/assets/carousel/quantum/hybrid-training.webp';
import imaginaryLight from '@/assets/carousel/chemistry/imaginary-mode-light.webp';
import imaginaryDark from '@/assets/carousel/chemistry/imaginary-mode-dark.webp';
import avogadroLight from '@/assets/carousel/chemistry/avogadro-light.webp';
import avogadroDark from '@/assets/carousel/chemistry/avogadro-dark.webp';

export interface SlideImage {
	src: string;
	srcSet: string;
}

export interface ScienceSlide {
	id: string;
	field: string;
	title: string;
	body: string;
	alt: string;
	link: { href: string; text: string };
	light: SlideImage;
	/** Present when the capture exists in both themes. */
	dark?: SlideImage;
}

interface SlideSource extends Omit<ScienceSlide, 'light' | 'dark'> {
	light: ImageMetadata;
	dark?: ImageMetadata;
}

const SLIDES: readonly SlideSource[] = [
	{
		id: 'stations',
		field: 'Earth science',
		title: 'GNSS stations on a globe',
		body: 'CLIO put 178 EarthScope GNSS stations across Cascadia on a globe, linked to a table of the stations. Pick a station in either one and the other follows.',
		alt: 'A CLIO dashboard titled Cascadia GNSS stations: a globe of western Washington with station dots and a hand-drawn deformation front on the left, a table of station IDs, names and coordinates on the right, with station CPXX selected in both',
		link: { href: '/docs/apps/#put-an-app-in-a-dashboard', text: 'Apps in a dashboard' },
		light: stationsLight,
		dark: stationsDark,
	},
	{
		id: 'helene',
		field: 'Atmospheric science',
		title: 'Hurricane Helene from ERA5',
		body: 'From ERA5 reanalysis in HDF5, CLIO followed the pressure minimum of Hurricane Helene and built a dashboard of its track, how it deepened and the sea surface temperature beneath it.',
		alt: 'A CLIO dashboard titled Hurricane Helene, 24-27 Sep 2024: headline numbers including 963.7 hPa lowest central pressure, a map of the hourly track from the Caribbean across the Gulf of Mexico into the southeastern United States, coloured by pressure, and charts of central pressure and sea surface temperature over time',
		link: { href: '/docs/examples/hdf5-paraview/', text: 'Explore a hurricane in HDF5 data' },
		light: heleneLight,
		dark: heleneDark,
	},
	{
		id: 'ocean-section',
		field: 'Oceanography',
		title: 'An Atlantic section and its water masses',
		body: 'From the 145 CTD casts of the 2013 A16N cruise, CLIO drew the section from south of Iceland to 6°S beside a T-S diagram of its water masses. Pick a cast in either view and the other picks it too.',
		alt: 'CLIO with two linked views: on the left a section of conservative temperature along the A16N cruise track against pressure to 6000 dbar, warm at the surface and cold in the deep; on the right a T-S diagram of conservative temperature against absolute salinity with density contours, its samples coloured by water mass, including Antarctic Bottom Water and Antarctic Intermediate Water',
		link: { href: '/docs/ocean-and-weather/', text: 'Ocean and weather science' },
		light: sectionLight,
		dark: sectionDark,
	},
	{
		id: 'paraview',
		field: 'Visualization',
		title: 'ParaView beside the conversation',
		body: 'CLIO opened a pressure-wave simulation in ParaView in a tab, stepped through its time steps and explained what the nested shells show and which way the wave travels.',
		alt: 'CLIO with a conversation about a pressure-wave simulation on the left and a ParaView tab on the right showing wave.pvd at step 12 of 12: nested coloured pressure shells inside a bounding box',
		link: { href: '/docs/connectors/#paraview', text: 'The ParaView plugin' },
		light: paraviewLight,
		dark: paraviewDark,
	},
	{
		id: 'cad',
		field: 'CAD',
		title: 'A part you can measure',
		body: 'CLIO wrote a two-part whistle as a STEP assembly. It opens as a 3D model you can measure, cut in section and pull apart into its parts.',
		alt: 'The STEP assembly whistle_assembly.step in the CLIO file viewer: an orange body and a blue plug, with three measurements labelled on the model, 81.85 mm, 3.00 mm and a 4.00 mm hole diameter, listed in the Measure panel',
		link: { href: '/docs/working-with-files/#view-3d-models', text: 'View 3D models' },
		light: measureLight,
		dark: measureDark,
	},
	{
		id: 'slicer',
		field: '3D printing',
		title: 'Slicing for a printer',
		body: 'CLIO sliced the whistle for a Prusa MK4 with PrusaSlicer and showed the result layer by layer, with print time and filament for each kind of extrusion and the slicer’s own warnings.',
		alt: 'A 3D-print slicer view in CLIO: the whistle sliced into layers, shown up to layer 61 of 79, with a panel listing print time 49 min 53 s, filament 10.62 g and the share of print time for each feature type, and a slicer warning about long bridging extrusions',
		link: { href: '/docs/connectors/#3d-print-slicer', text: 'The 3D-print slicer' },
		light: slicerLight,
		dark: slicerDark,
	},
	{
		id: 'electronics',
		field: 'Electronics',
		title: 'A circuit board in KiCad',
		body: 'For a small USB-C sensor node, CLIO routed the board in KiCad. The live board opens in KiCad’s view inside CLIO, where CLIO can run the design rule check.',
		alt: 'CLIO with the sensor-node conversation on the left, where the agent reports the routed board (231 tracks, 13 vias, 19 footprints) and runs the design rule check, and the KiCad view open in a tab on the right, marked Ready, showing the routed board with red and blue copper tracks between the USB-C connector, the ESP32-C3 module and the other parts',
		link: { href: '/docs/kicad/', text: 'Design a circuit board with KiCad' },
		light: routedBoard,
	},
	{
		id: 'enclosure',
		field: 'Electronics',
		title: 'A case for the board',
		body: 'CLIO designed a 3D-printable case around the measured board. Here it is see-through, with the board inside, the USB-C opening at the left and holes in the lid for the LED, button and sensor.',
		alt: 'A see-through 3D render titled Assembled: a rounded grey case with screw posts at its corners and the green sensor-node board inside, with its USB-C connector, ESP32-C3 module and small parts visible',
		link: { href: '/docs/kicad/', text: 'Design a circuit board with KiCad' },
		light: boardCase,
	},
	{
		id: 'quantum',
		field: 'Quantum computing',
		title: 'A hybrid quantum classifier',
		body: 'CLIO trained a hybrid quantum-classical classifier beside a classical baseline on the same MNIST digits and charted loss and accuracy for both, epoch by epoch.',
		alt: 'Two charts from a CLIO training run on MNIST 0 vs 1: loss per epoch and training accuracy per epoch over 30 epochs, comparing a classical model and a hybrid quantum-classical model',
		link: { href: '/docs/examples/hybrid-qml/', text: 'A hybrid quantum classifier' },
		light: hybridTraining,
	},
	{
		id: 'imaginary-mode',
		field: 'Chemistry',
		title: 'An imaginary mode you can watch',
		body: 'Asked for eclipsed ethane, CLIO optimized it with MACE, found its one imaginary frequency, 287i cm⁻¹, and played that mode in the 3D viewer beside the conversation: the torsion that leads to the staggered form.',
		alt: 'CLIO with the ethane conversation on the left, reporting the MACE optimization and one imaginary frequency at 287.3i cm⁻¹, and the Burette 3D viewer open in a tab on the right, playing the mode: ethane with its two methyl groups twisting about the carbon-carbon bond',
		link: { href: '/docs/examples/chemistry/', text: 'Computational chemistry with Alembic' },
		light: imaginaryLight,
		dark: imaginaryDark,
	},
	{
		id: 'avogadro',
		field: 'Chemistry',
		title: 'Avogadro in the conversation',
		body: 'CLIO built ethylene glycol, optimized it with MACE and opened it in Avogadro 2, whose full window runs in a tab beside the conversation, ready for editing.',
		alt: 'CLIO with the ethylene glycol conversation on the left, reporting the MACE optimization with a table of bond lengths, and an Avogadro tab on the right filled by the Avogadro 2 window at full size, menus and display panels included, with ethylene glycol as balls and sticks',
		link: { href: '/docs/examples/chemistry/', text: 'Computational chemistry with Alembic' },
		light: avogadroLight,
		dark: avogadroDark,
	},
];

const WIDTHS = [640, 960, 1280, 1600];

async function responsive(source: ImageMetadata): Promise<SlideImage> {
	const widths = [...new Set([...WIDTHS.filter((w) => w < source.width), source.width])];
	const image = await getImage({ src: source, format: 'webp', widths, quality: 80 });
	return { src: image.src, srcSet: image.srcSet.attribute };
}

/** Resolve every slide's images for the page. */
export async function scienceSlides(): Promise<ScienceSlide[]> {
	return Promise.all(
		SLIDES.map(async ({ light, dark, ...slide }) => ({
			...slide,
			light: await responsive(light),
			...(dark ? { dark: await responsive(dark) } : {}),
		})),
	);
}
