mod duet;

use std::sync::{Mutex, MutexGuard};

use duet::{Change, DeviceState, Duet, Error};
use tauri::State;

/// The open device, if any. Commands hold the lock for a whole USB exchange,
/// so writes reach the device in the order the panel sent them.
#[derive(Default)]
struct Panel(Mutex<Option<Duet>>);

impl Panel {
  fn lock(&self) -> MutexGuard<'_, Option<Duet>> {
    self.0.lock().unwrap_or_else(|poisoned| poisoned.into_inner())
  }

  /// Runs one exchange with the open device, dropping it once it is gone.
  fn with<F>(&self, exchange: F) -> Result<DeviceState, Error>
  where
    F: FnOnce(&mut Duet) -> Result<&DeviceState, Error>,
  {
    let mut slot = self.lock();
    let duet = slot.as_mut().ok_or(Error::Disconnected)?;
    match exchange(duet) {
      Ok(state) => Ok(state.clone()),
      Err(error) => {
        if error.is_fatal() {
          *slot = None;
        }
        Err(error)
      }
    }
  }
}

/// Opens the device, or re-reads every register when it is already open.
#[tauri::command]
async fn connect(panel: State<'_, Panel>) -> Result<DeviceState, Error> {
  let mut slot = panel.lock();
  if let Some(duet) = slot.as_mut() {
    match duet.refresh() {
      Ok(state) => return Ok(state.clone()),
      // A stale handle (replugged or power cycled device) is reopened below.
      Err(_) => *slot = None,
    }
  }
  let duet = Duet::open()?;
  let state = duet.state().clone();
  *slot = Some(duet);
  Ok(state)
}

#[tauri::command]
async fn apply(panel: State<'_, Panel>, change: Change) -> Result<DeviceState, Error> {
  panel.with(|duet| duet.apply(change))
}

#[tauri::command]
async fn poll(panel: State<'_, Panel>) -> Result<DeviceState, Error> {
  panel.with(Duet::poll)
}

fn main() {
  // WebKitGTK's DMA-BUF renderer paints blank windows on NVIDIA and on some
  // Wayland compositors; the panel has nothing to gain from it.
  if std::env::var_os("WEBKIT_DISABLE_DMABUF_RENDERER").is_none() {
    // SAFETY: no other thread exists yet.
    unsafe { std::env::set_var("WEBKIT_DISABLE_DMABUF_RENDERER", "1") };
  }

  tauri::Builder::default()
    .manage(Panel::default())
    .invoke_handler(tauri::generate_handler![connect, apply, poll])
    .run(tauri::generate_context!())
    .expect("failed to start the Tauri application");
}
