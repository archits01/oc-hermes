export interface RestoreMessageTarget {
  /** Durable gateway row id for the turn, when it has one. Carried alongside
   *  the message id because the id is re-minted on every transcript rebuild
   *  and the confirm dialog holds its capture across background polls. */
  rowId?: number
  text: string
  userOrdinal: number | null
}
