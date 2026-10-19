// The MPC Fill tab: art downloaded from MPC Fill, apart from the user's own
// uploads on the Customs tab. The same library and component (see
// CustomsPage.tsx); this view searches MPC Fill instead of taking uploads,
// and Add to Project puts the art on its Scryfall card (mpcLibrary.ts).
import { CustomLibraryPage } from "./CustomsPage";

export default function MpcFillPage() {
  return <CustomLibraryPage view="mpc" />;
}
